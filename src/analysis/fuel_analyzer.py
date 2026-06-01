"""Análise de consumo e diagnóstico de eficiência — Logan 1.0L D4F/K7M.

Método de estimativa: Speed Density (MAP + RPM + IAT), pois o Logan
não tem sensor MAF (PID 0x10 ausente no bitmap).

Especificações do motor:
  - D4F  1.0L  3 cilindros  75cv  (versão 16v)
  - K7M  1.6L  4 cilindros  82cv  (versão 8v)
  - Combustível Brasil: E25 (gasolina comum) ou E100 (etanol)
  - AFR estequiométrico E25: ~13.5 | E100: ~9.0
  - Densidade E25: ~730 g/L | E100: ~789 g/L
"""

from __future__ import annotations

import statistics
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Optional


# ---------------------------------------------------------------------------
# Constantes do motor
# ---------------------------------------------------------------------------

ENGINES = {
    "D4F_1.0": {"displacement_L": 0.999, "cylinders": 3, "name": "1.0L D4F 16v"},
    "K7M_1.6": {"displacement_L": 1.598, "cylinders": 4, "name": "1.6L K7M 8v"},
}

FUELS = {
    "E25":  {"afr": 13.5, "density_g_L": 730,  "label": "Gasolina (E25)"},
    "E27":  {"afr": 13.2, "density_g_L": 730,  "label": "Gasolina aditivada (E27)"},
    "E100": {"afr": 9.0,  "density_g_L": 789,  "label": "Etanol (E100)"},
}


# ---------------------------------------------------------------------------
# Estrutura de amostra
# ---------------------------------------------------------------------------

@dataclass
class FuelSample:
    timestamp: float
    rpm:           Optional[float] = None   # rpm
    speed_kmh:     Optional[float] = None   # km/h
    map_kpa:       Optional[float] = None   # kPa
    iat_c:         Optional[float] = None   # °C
    coolant_c:     Optional[float] = None   # °C
    engine_load:   Optional[float] = None   # %
    throttle:      Optional[float] = None   # %
    stft:          Optional[float] = None   # %
    ltft:          Optional[float] = None   # %
    o2_b1s1:       Optional[float] = None   # V
    o2_b1s2:       Optional[float] = None   # V
    timing_adv:    Optional[float] = None   # graus antes PMS
    bat_voltage:   Optional[float] = None   # V
    # Renault Mode 21 (opcional)
    inj_time_ms:   Optional[float] = None
    lambda_mv:     Optional[float] = None

    # Calculados após ingestão
    fuel_flow_g_s: Optional[float] = None
    instant_L100:  Optional[float] = None


# ---------------------------------------------------------------------------
# Estimador de consumo (Speed Density)
# ---------------------------------------------------------------------------

class FuelEstimator:
    """Estima fluxo de combustível via speed density (MAP + RPM + IAT).

    Sem sensor MAF: usamos equação dos gases ideais para calcular
    a massa de ar admitida e dividimos pelo AFR estequiométrico.
    """

    _R_AIR = 287.0          # J/(kg·K) — constante específica do ar

    def __init__(self, engine: str = "D4F_1.0", fuel: str = "E25"):
        cfg = ENGINES.get(engine, ENGINES["D4F_1.0"])
        self._disp_m3    = cfg["displacement_L"] * 0.001          # m³
        self._fuel_cfg   = FUELS.get(fuel, FUELS["E25"])
        self._VE_base    = 0.78                                    # eficiência vol. base

    def set_fuel(self, fuel: str) -> None:
        self._fuel_cfg = FUELS.get(fuel, FUELS["E25"])

    def estimate(self, sample: FuelSample) -> FuelSample:
        """Preenche fuel_flow_g_s e instant_L100 na amostra."""
        rpm  = sample.rpm
        mp   = sample.map_kpa
        iat  = sample.iat_c
        spd  = sample.speed_kmh
        stft = sample.stft or 0.0
        ltft = sample.ltft or 0.0

        if rpm is None or mp is None or rpm < 400:
            sample.fuel_flow_g_s = None
            sample.instant_L100  = None
            return sample

        # Se temos tempo de injeção direto (Mode 21), é mais preciso
        if sample.inj_time_ms is not None:
            # injectors D4F: ~200 cc/min de gasolina = ~2.4 g/s a 100%
            injector_g_s = 2.4
            duty = min(sample.inj_time_ms * rpm / 120_000, 1.0)
            fuel_g_s = injector_g_s * duty * 3  # 3 cilindros
        else:
            # Speed density
            T_K = (iat + 273.15) if iat is not None else 308.0
            # Eficiência volumétrica: aumenta com MAP (aprox linear)
            VE = self._VE_base + (mp / 100.0) * 0.15
            VE = min(VE, 0.95)

            # Ciclos/s = RPM / 60 / 2  (4-stroke: 1 admissão a cada 2 rotações)
            cycles_s = rpm / 120.0

            # Massa de ar (kg/s) = P × V_disp × ciclos × VE / (R × T)
            air_kg_s = (mp * 1000 * self._disp_m3 * cycles_s * VE) / (self._R_AIR * T_K)

            # Correção pelos fuel trims
            trim_factor = 1.0 + (stft + ltft) / 100.0

            # Combustível (g/s)
            afr = self._fuel_cfg["afr"]
            fuel_g_s = (air_kg_s * 1000 / afr) * trim_factor
            fuel_g_s = max(0.0, fuel_g_s)

        sample.fuel_flow_g_s = fuel_g_s

        # Consumo instantâneo L/100km (só quando em movimento)
        if spd and spd > 3.0:
            fuel_L_h = fuel_g_s * 3600 / self._fuel_cfg["density_g_L"]
            sample.instant_L100 = fuel_L_h / spd * 100.0
        else:
            sample.instant_L100 = None

        return sample


# ---------------------------------------------------------------------------
# Resultado de diagnóstico
# ---------------------------------------------------------------------------

@dataclass
class Diagnosis:
    code: str
    severity: str       # "INFO" | "AVISO" | "CRITICO"
    title: str
    description: str
    evidence: str
    causes: list[str]
    actions: list[str]
    icon: str = "⚠"     # emoji de alerta para a UI


# ---------------------------------------------------------------------------
# Detector de falhas (regras estáticas + médias da sessão)
# ---------------------------------------------------------------------------

class IssueDetector:
    """Avalia amostras acumuladas e retorna diagnósticos."""

    # Janela rolante para análise de variação (O2 ativo/lento)
    _O2_WINDOW = 60   # amostras

    def __init__(self):
        self._o2_history: deque[float] = deque(maxlen=self._O2_WINDOW)

    def feed(self, s: FuelSample) -> list[Diagnosis]:
        """Processa uma amostra e retorna diagnósticos imediatos (se houver)."""
        diags: list[Diagnosis] = []
        if s.o2_b1s1 is not None:
            self._o2_history.append(s.o2_b1s1)

        # Diagnósticos instantâneos (acionados por amostra individual)
        if s.bat_voltage is not None and s.bat_voltage < 12.0:
            diags.append(Diagnosis(
                code="LOW_VOLTAGE",
                severity="AVISO",
                title="Tensão da bateria baixa",
                description=f"Bateria: {s.bat_voltage:.1f}V (mínimo recomendado: 12.0V)",
                evidence=f"PID 0142 = {s.bat_voltage:.2f}V",
                causes=["Bateria envelhecida ou descarregada", "Alternador com falha"],
                actions=["Testar bateria com voltímetro", "Verificar alternador"],
                icon="🔋",
            ))

        return diags

    def analyze_session(self, samples: list[FuelSample], fuel: str = "E25") -> list[Diagnosis]:
        """Análise pós-sessão: avalia médias, tendências e padrões."""
        diags: list[Diagnosis] = []
        if not samples:
            return diags

        # Filtra amostras em movimento (velocidade > 5 km/h) e com motor quente (temp > 70°C)
        moving  = [s for s in samples if (s.speed_kmh or 0) > 5]
        hot     = [s for s in samples if (s.coolant_c or 0) > 70]
        highway = [s for s in samples if (s.speed_kmh or 0) > 60]

        def avg(lst, attr):
            vals = [getattr(x, attr) for x in lst if getattr(x, attr) is not None]
            return statistics.mean(vals) if vals else None

        def pct_above(lst, attr, threshold):
            vals = [getattr(x, attr) for x in lst if getattr(x, attr) is not None]
            return sum(1 for v in vals if v > threshold) / max(len(vals), 1) * 100

        # ── LTFT / mistura ──────────────────────────────────────────────
        avg_ltft = avg(hot, "ltft")
        avg_stft = avg(hot, "stft")

        if avg_ltft is not None:
            total_trim = avg_ltft + (avg_stft or 0)
            if total_trim < -8:
                diags.append(Diagnosis(
                    code="RICH_MIXTURE",
                    severity="CRITICO",
                    title="Mistura rica — consumo aumentado",
                    description=(
                        f"LTFT médio = {avg_ltft:.1f}% e STFT = {avg_stft or 0:.1f}%. "
                        "Motor está injetando combustível em excesso. "
                        "Cada -1% de LTFT ≈ +1% de consumo."
                    ),
                    evidence=f"LTFT: {avg_ltft:.1f}%  STFT: {avg_stft or 0:.1f}%",
                    causes=[
                        "Sonda lambda com defeito (resposta lenta ou tensão travada)",
                        "Injetor com vazamento interno (gotejando em repouso)",
                        "Sensor de temperatura do ar (IAT) descalibrado",
                        "Válvula de evaporação (canister) aberta constantemente",
                        "Sensor MAP com leitura incorreta",
                    ],
                    actions=[
                        f"Verificar oscilação da sonda O2 (deve variar 0.1V–0.9V rapidamente)",
                        "Checar pressão de combustível (especificação: ~3.0 bar)",
                        "Teste de vazamento de injetor (motor desligado, verifique gota)",
                        "Limpar/verificar o canister de evaporação",
                    ],
                    icon="🔴",
                ))
            elif total_trim > 10:
                diags.append(Diagnosis(
                    code="LEAN_MIXTURE",
                    severity="CRITICO",
                    title="Mistura pobre — motor compensando",
                    description=(
                        f"LTFT médio = {avg_ltft:.1f}%. Motor está corrigindo para mais "
                        "combustível, indicando entrada de ar não medida."
                    ),
                    evidence=f"LTFT: {avg_ltft:.1f}%",
                    causes=[
                        "Vazamento de vácuo (mangueiras rachadas, junta do coletor)",
                        "Filtro de ar muito entupido",
                        "Sensor MAP com leitura baixa",
                        "Bico injetor parcialmente entupido",
                    ],
                    actions=[
                        "Inspecionar todas as mangueiras de vácuo (especialmente no coletor)",
                        "Substituir filtro de ar",
                        "Verificar vedação do corpo do acelerador",
                    ],
                    icon="🟠",
                ))
            elif 3 < avg_ltft < 8:
                diags.append(Diagnosis(
                    code="TRIM_LEAN_MILD",
                    severity="AVISO",
                    title="Leve tendência para mistura pobre",
                    description=f"LTFT = {avg_ltft:.1f}% (ideal: -5% a +5%). Monitorar.",
                    evidence=f"LTFT médio: {avg_ltft:.1f}%",
                    causes=["Filtro de ar com restrição moderada", "Pequeno vazamento de vácuo"],
                    actions=["Trocar filtro de ar", "Inspecionar mangueiras de vácuo"],
                    icon="🟡",
                ))
            elif -8 < avg_ltft < -3:
                diags.append(Diagnosis(
                    code="TRIM_RICH_MILD",
                    severity="AVISO",
                    title="Leve tendência para mistura rica",
                    description=f"LTFT = {avg_ltft:.1f}%. Ligeiramente fora do ideal.",
                    evidence=f"LTFT médio: {avg_ltft:.1f}%",
                    causes=["Sonda lambda envelhecendo", "Pequeno vazamento de injetor"],
                    actions=["Verificar oscilação da sonda O2", "Limpeza de injetores"],
                    icon="🟡",
                ))

        # ── Temperatura do motor ─────────────────────────────────────────
        max_coolant  = max((s.coolant_c for s in samples if s.coolant_c), default=None)
        avg_coolant  = avg(samples[len(samples)//2:], "coolant_c")  # segunda metade
        runtime_s    = (samples[-1].timestamp - samples[0].timestamp) if len(samples) > 1 else 0

        if max_coolant is not None and max_coolant < 82 and runtime_s > 300:
            diags.append(Diagnosis(
                code="COLD_ENGINE",
                severity="CRITICO",
                title="Termostato provavelmente defeituoso (preso aberto)",
                description=(
                    f"Temperatura máxima atingida: {max_coolant:.0f}°C após "
                    f"{runtime_s/60:.0f} min. Motor operando sempre em malha aberta "
                    "(open loop = mistura rica fixa). Causa direta de alto consumo."
                ),
                evidence=f"Temp. máx: {max_coolant:.0f}°C  |  Tempo: {runtime_s/60:.0f} min",
                causes=[
                    "Termostato preso na posição aberta",
                    "Sensor de temperatura com defeito (ECT)",
                ],
                actions=[
                    "Substituir termostato (peça simples, custo baixo, impacto enorme no consumo)",
                    "Verificar sensor ECT com multímetro (NTC: ~2.5 kΩ a 20°C, ~300 Ω a 80°C)",
                ],
                icon="🌡️",
            ))
        elif avg_coolant is not None and avg_coolant < 88 and runtime_s > 600:
            diags.append(Diagnosis(
                code="COOL_ENGINE",
                severity="AVISO",
                title="Motor não atinge temperatura ideal",
                description=f"Temperatura média na 2ª metade da viagem: {avg_coolant:.0f}°C (ideal: 90–95°C).",
                evidence=f"Temp. média: {avg_coolant:.0f}°C",
                causes=["Termostato com abertura prematura", "Viagem muito curta"],
                actions=["Verificar termostato", "Evitar viagens muito curtas"],
                icon="🌡️",
            ))

        # ── Avanço de ignição ────────────────────────────────────────────
        # Só analisamos sob carga média (MAP > 50 kPa, RPM > 1500)
        under_load = [s for s in hot if (s.map_kpa or 0) > 50 and (s.rpm or 0) > 1500]
        avg_timing = avg(under_load, "timing_adv")

        if avg_timing is not None and avg_timing < 8 and len(under_load) > 10:
            diags.append(Diagnosis(
                code="TIMING_RETARD",
                severity="CRITICO",
                title="Ignição muito atrasada — possível detonação (knock)",
                description=(
                    f"Avanço médio sob carga: {avg_timing:.1f}° (ideal: 15–25° nessa faixa). "
                    "ECU retardando ignição para evitar detonação, reduzindo eficiência."
                ),
                evidence=f"Avanço médio sob carga: {avg_timing:.1f}°",
                causes=[
                    "Combustível de baixa octanagem",
                    "Sensor de detonação (knock) com defeito",
                    "Velas de ignição gastas ou com folga incorreta",
                    "Acúmulo de carbono nas câmaras de combustão",
                    "Temperatura de operação muito alta",
                ],
                actions=[
                    "Usar combustível de melhor qualidade (92+ octanas)",
                    "Verificar e trocar velas de ignição (intervalo: 30.000 km D4F)",
                    "Verificar sensor de detonação (ouvir batida em aceleração)",
                    "Limpeza de câmaras via aditivo ou carbono físico",
                ],
                icon="⚡",
            ))
        elif avg_timing is not None and avg_timing < 14 and len(under_load) > 10:
            diags.append(Diagnosis(
                code="TIMING_LOW",
                severity="AVISO",
                title="Avanço de ignição abaixo do ideal",
                description=f"Avanço médio: {avg_timing:.1f}° — margem para melhoria.",
                evidence=f"Avanço médio: {avg_timing:.1f}°",
                causes=["Combustível comum", "Velas envelhecidas"],
                actions=["Tentar combustível com mais octanagem", "Verificar velas"],
                icon="🟡",
            ))

        # ── Sonda Lambda (O2 B1S1) ───────────────────────────────────────
        o2_vals = [s.o2_b1s1 for s in hot if s.o2_b1s1 is not None and (s.rpm or 0) > 600]
        if len(o2_vals) > 30:
            o2_range = max(o2_vals) - min(o2_vals)
            o2_mean  = statistics.mean(o2_vals)

            if o2_range < 0.3:
                diags.append(Diagnosis(
                    code="O2_LAZY",
                    severity="CRITICO" if o2_range < 0.15 else "AVISO",
                    title="Sonda lambda lenta ou com defeito",
                    description=(
                        f"Variação da sonda: {o2_range:.2f}V (mínimo saudável: 0.5V). "
                        "Sonda saudável deve oscilar rapidamente entre ~0.1V e ~0.9V em malha fechada."
                    ),
                    evidence=f"O2 min={min(o2_vals):.2f}V  max={max(o2_vals):.2f}V  amplitude={o2_range:.2f}V",
                    causes=[
                        "Sonda lambda envelhecida (vida útil: ~80.000 km)",
                        "Sonda com contaminação (silício, anticongelante)",
                        "Sonda fria (aquecedor interno com defeito)",
                        "ECU operando em malha aberta (motor frio ou falha de sinal)",
                    ],
                    actions=[
                        "Substituir sonda lambda (sonda Bosch universal para D4F é compatível)",
                        "Verificar fiação e conector da sonda (tensão de aquecedor: ~12V)",
                    ],
                    icon="🔴",
                ))

            if o2_mean > 0.65 and len(o2_vals) > 30:
                diags.append(Diagnosis(
                    code="O2_RICH_BIAS",
                    severity="AVISO",
                    title="Sonda O2 indicando mistura rica persistente",
                    description=f"Tensão média O2 = {o2_mean:.2f}V (ideal em malha fechada: ~0.45V).",
                    evidence=f"O2 B1S1 média: {o2_mean:.2f}V",
                    causes=["Injetor com vazamento", "Pressão de combustível alta", "Sensor MAP incorreto"],
                    actions=["Verificar pressão de combustível", "Teste de injetores"],
                    icon="🟠",
                ))

        # ── P0101 / MAP vs Carga (detecção para motor sem MAF) ─────────
        # Para o Logan 1.0 D4F (sem MAF físico), o ECU usa MAP+IAT+RPM
        # para calcular carga virtual. Se MAP e carga divergem muito,
        # indica sensor MAP com desvio ou vazamento de coletor.
        map_load_pairs = [
            (s.map_kpa, s.engine_load)
            for s in hot
            if s.map_kpa is not None and s.engine_load is not None
            and (s.rpm or 0) > 800
        ]
        if len(map_load_pairs) > 20:
            # Em marcha lenta: MAP ~35-50 kPa → carga ~20-40%
            # Em carga parcial: MAP ~60-80 kPa → carga ~50-75%
            # Desvio grande indica sensor MAP ou vazamento de vácuo
            idle_pairs = [(m, l) for m, l in map_load_pairs if m < 55]
            if idle_pairs:
                avg_idle_map = statistics.mean(m for m, l in idle_pairs)
                avg_idle_load = statistics.mean(l for m, l in idle_pairs)
                # MAP em marcha lenta deveria ser 30-55 kPa para 1.0L
                if avg_idle_map > 60:
                    diags.append(Diagnosis(
                        code="MAP_HIGH_IDLE",
                        severity="AVISO",
                        title="MAP elevado em marcha lenta (possível P0101)",
                        description=(
                            f"MAP médio em idle: {avg_idle_map:.0f} kPa "
                            f"(carga: {avg_idle_load:.0f}%). "
                            "Para Logan 1.0, MAP em marcha lenta deveria ser 30–50 kPa. "
                            "Valor alto indica vazamento de admissão ou sensor MAP incorreto. "
                            "P0101 ativo = algoritmo MAF virtual detectou inconsistência."
                        ),
                        evidence=f"MAP idle: {avg_idle_map:.0f} kPa | Carga idle: {avg_idle_load:.0f}%",
                        causes=[
                            "Sensor MAP com leitura alta (calibração incorreta ou sujo)",
                            "Vazamento na borracha do corpo do acelerador",
                            "Junta do coletor de admissão com micro-trinca",
                            "Mangueira de vácuo do MAP solta/rachada",
                            "Filtro de ar obstruído (aumenta pressão de admissão)",
                        ],
                        actions=[
                            "Verificar e trocar mangueira de vácuo do sensor MAP",
                            "Inspecionar vedação do corpo do acelerador (checar com spray de carburetor)",
                            "Limpar o sensor MAP com spray de sensor MAF",
                            "Substituir filtro de ar",
                            "Verificar junta do coletor de admissão",
                        ],
                        icon="📡",
                    ))

        # ── Carga do motor na estrada ────────────────────────────────────
        avg_load_hwy = avg(highway, "engine_load")
        avg_spd_hwy  = avg(highway, "speed_kmh")

        if avg_load_hwy and avg_spd_hwy and avg_load_hwy > 72:
            diags.append(Diagnosis(
                code="HIGH_LOAD_HIGHWAY",
                severity="AVISO",
                title="Carga do motor muito alta na estrada",
                description=(
                    f"Carga média em velocidade de estrada ({avg_spd_hwy:.0f} km/h): "
                    f"{avg_load_hwy:.0f}%. Para 1.0L em marcha alta, esperado: 40–65%."
                ),
                evidence=f"Carga média: {avg_load_hwy:.0f}%  @  {avg_spd_hwy:.0f} km/h",
                causes=[
                    "Marcha baixa (usando 4ª no lugar de 5ª)",
                    "Pressão dos pneus baixa (aumenta resistência de rolamento significativamente)",
                    "Freio de estacionamento parcialmente preso",
                    "Embreagem patinando (perde eficiência de transmissão)",
                    "Filtro de ar muito entupido",
                ],
                actions=[
                    "Verificar calibragem dos pneus (Logan 1.0: dianteiro 30 psi, traseiro 27 psi)",
                    "Verificar freio de estacionamento",
                    "Tentar andar em marcha mais alta possível",
                ],
                icon="📈",
            ))

        # ── RPM vs Velocidade (relação de transmissão) ───────────────────
        gear5_samples = [s for s in highway if (s.speed_kmh or 0) > 80 and (s.rpm or 0) > 0]
        if len(gear5_samples) > 20:
            ratios = [(s.rpm / s.speed_kmh) for s in gear5_samples if s.speed_kmh > 10]
            avg_ratio = statistics.mean(ratios)
            # Logan 1.0 em 5ª a 100 km/h ≈ 2900-3200 RPM → ratio ≈ 29-32
            if avg_ratio > 38:
                diags.append(Diagnosis(
                    code="WRONG_GEAR",
                    severity="INFO",
                    title="RPM alto para a velocidade (marcha abaixo do ideal)",
                    description=(
                        f"Relação média: {avg_ratio:.0f} RPM/(km/h). "
                        "Logan 1.0 em 5ª a 100 km/h deve ter ~3000 RPM (ratio ≈ 30)."
                    ),
                    evidence=f"RPM médio / velocidade média = {avg_ratio:.0f}",
                    causes=["Viajando em 4ª ao invés de 5ª", "Embreagem patinando (ratio alto mesmo em 5ª)"],
                    actions=["Usar 5ª marcha na estrada acima de 70 km/h"],
                    icon="⚙️",
                ))

        # ── Tensão da bateria em movimento ─────────────────────────────
        bat_vals = [s.bat_voltage for s in moving if s.bat_voltage is not None]
        if bat_vals:
            avg_bat = statistics.mean(bat_vals)
            if avg_bat < 13.8:
                diags.append(Diagnosis(
                    code="ALTERNATOR",
                    severity="AVISO",
                    title="Alternador possivelmente fraco",
                    description=(
                        f"Tensão média com motor em marcha: {avg_bat:.1f}V "
                        "(alternador saudável deve manter 13.8–14.5V)."
                    ),
                    evidence=f"Tensão média em marcha: {avg_bat:.1f}V",
                    causes=["Alternador desgastado", "Correia do alternador frouxa", "Bateria com sulfatação"],
                    actions=["Verificar correia e tensionador do alternador", "Testar alternador"],
                    icon="⚡",
                ))

        # ── Sem problemas detectados ─────────────────────────────────────
        if not diags:
            diags.append(Diagnosis(
                code="OK",
                severity="INFO",
                title="Nenhuma falha crítica detectada nesta sessão",
                description="Parâmetros dentro dos limites normais. Verifique manutenção preventiva.",
                evidence="",
                causes=[],
                actions=[
                    "Verificar intervalo de troca de velas (D4F: 30.000 km)",
                    "Verificar filtro de ar (a cada 15.000 km)",
                    "Pressão dos pneus: 30 psi dianteiro / 27 psi traseiro",
                ],
                icon="✅",
            ))

        return diags


# ---------------------------------------------------------------------------
# Sessão de análise
# ---------------------------------------------------------------------------

@dataclass
class AnalysisSession:
    fuel_type: str = "E25"
    engine:    str = "D4F_1.0"

    samples:   list[FuelSample]  = field(default_factory=list)
    issues:    list[Diagnosis]   = field(default_factory=list)

    # Acumuladores
    _fuel_used_g: float = field(default=0.0, init=False)
    _last_ts:     float = field(default=0.0, init=False)
    _estimator:   FuelEstimator = field(init=False)
    _detector:    IssueDetector = field(init=False)

    def __post_init__(self):
        self._estimator = FuelEstimator(self.engine, self.fuel_type)
        self._detector  = IssueDetector()

    # ── ingestão de dados ──────────────────────────────────────────────

    def ingest(self, sample: FuelSample) -> list[Diagnosis]:
        """Adiciona amostra, estima consumo e retorna alertas imediatos."""
        self._estimator.estimate(sample)
        self.samples.append(sample)

        # Acumula combustível usado
        if sample.fuel_flow_g_s is not None and self._last_ts > 0:
            dt = sample.timestamp - self._last_ts
            self._fuel_used_g += sample.fuel_flow_g_s * dt
        self._last_ts = sample.timestamp

        return self._detector.feed(sample)

    # ── agregados da sessão ────────────────────────────────────────────

    @property
    def duration_s(self) -> float:
        if len(self.samples) < 2:
            return 0.0
        return self.samples[-1].timestamp - self.samples[0].timestamp

    @property
    def distance_km(self) -> float:
        """Distância integrada pela velocidade."""
        dist = 0.0
        for i in range(1, len(self.samples)):
            dt = self.samples[i].timestamp - self.samples[i - 1].timestamp
            v = (self.samples[i - 1].speed_kmh or 0.0)
            dist += v * dt / 3600.0
        return dist

    @property
    def fuel_used_L(self) -> float:
        density = FUELS.get(self.fuel_type, FUELS["E25"])["density_g_L"]
        return self._fuel_used_g / density

    @property
    def avg_consumption_L100(self) -> Optional[float]:
        dist = self.distance_km
        if dist < 0.1:
            return None
        return self.fuel_used_L / dist * 100.0

    @property
    def avg_speed_kmh(self) -> Optional[float]:
        vals = [s.speed_kmh for s in self.samples if s.speed_kmh is not None]
        return statistics.mean(vals) if vals else None

    # ── análise final ──────────────────────────────────────────────────

    def finalize(self) -> list[Diagnosis]:
        """Roda análise completa no final da sessão."""
        self.issues = self._detector.analyze_session(self.samples, self.fuel_type)
        return self.issues

    def _sensor_summary(self) -> dict:
        """Calcula estatísticas rápidas dos sensores para o relatório."""
        def avg_attr(attr, filt=None):
            lst = self.samples if filt is None else [s for s in self.samples if filt(s)]
            vals = [getattr(s, attr) for s in lst if getattr(s, attr) is not None]
            return statistics.mean(vals) if vals else None

        hot = [s for s in self.samples if (s.coolant_c or 0) > 60]
        return {
            "coolant_max": max((s.coolant_c for s in self.samples if s.coolant_c), default=None),
            "coolant_avg": avg_attr("coolant_c"),
            "ltft_avg":    avg_attr("ltft", lambda s: (s.coolant_c or 0) > 60),
            "stft_avg":    avg_attr("stft", lambda s: (s.coolant_c or 0) > 60),
            "timing_avg":  avg_attr("timing_adv", lambda s: (s.map_kpa or 0) > 50 and (s.rpm or 0) > 1500),
            "o2_min":      min((s.o2_b1s1 for s in hot if s.o2_b1s1 is not None), default=None),
            "o2_max":      max((s.o2_b1s1 for s in hot if s.o2_b1s1 is not None), default=None),
            "map_avg":     avg_attr("map_kpa"),
            "rpm_avg":     avg_attr("rpm"),
            "speed_max":   max((s.speed_kmh for s in self.samples if s.speed_kmh), default=None),
            "bat_avg":     avg_attr("bat_voltage"),
        }

    def export_txt(self, path: str) -> None:
        """Exporta relatório em texto legível e compartilhável."""
        fuel_label = FUELS.get(self.fuel_type, FUELS["E25"])["label"]
        engine_label = ENGINES.get(self.engine, ENGINES["D4F_1.0"])["name"]
        stats = self._sensor_summary()

        lines = [
            "=" * 60,
            " RELATÓRIO DE ANÁLISE DE CONSUMO — RENAULT LOGAN",
            "=" * 60,
            f"Motor: {engine_label}",
            f"Combustível: {fuel_label}",
            f"Duração: {self.duration_s / 60:.1f} min",
            f"Distância estimada: {self.distance_km:.2f} km",
            f"Combustível estimado: {self.fuel_used_L:.2f} L",
        ]
        if self.avg_consumption_L100 is not None:
            lines.append(f"Consumo médio estimado: {self.avg_consumption_L100:.1f} L/100km "
                         f"({100/self.avg_consumption_L100:.1f} km/L)")
        if self.avg_speed_kmh is not None:
            lines.append(f"Velocidade média: {self.avg_speed_kmh:.0f} km/h")

        # ── Estatísticas dos sensores ────────────────────────────────────
        lines.append("")
        lines.append("SENSORES (médias da sessão):")
        lines.append("-" * 40)
        if stats["coolant_max"] is not None:
            lines.append(f"Temp. refrigerante: máx {stats['coolant_max']:.0f}°C  "
                         f"| média {stats['coolant_avg'] or 0:.0f}°C")
        if stats["ltft_avg"] is not None:
            lines.append(f"LTFT (longo prazo):  {stats['ltft_avg']:+.1f}%  "
                         f"| STFT: {stats['stft_avg'] or 0:+.1f}%")
        if stats["timing_avg"] is not None:
            lines.append(f"Avanço ignição (sob carga): {stats['timing_avg']:+.1f}°")
        if stats["o2_min"] is not None:
            lines.append(f"Sonda O2 B1S1: {stats['o2_min']:.2f}V–{stats['o2_max']:.2f}V "
                         f"(amplitude: {stats['o2_max'] - stats['o2_min']:.2f}V)")
        if stats["map_avg"] is not None:
            lines.append(f"MAP médio: {stats['map_avg']:.0f} kPa")
        if stats["rpm_avg"] is not None:
            lines.append(f"RPM médio: {stats['rpm_avg']:.0f}")
        if stats["bat_avg"] is not None:
            lines.append(f"Tensão bateria: {stats['bat_avg']:.1f}V")

        lines.append("")
        lines.append("DIAGNÓSTICOS:")
        lines.append("-" * 40)
        if not self.issues:
            lines.append("Nenhuma falha detectada.")
        for d in self.issues:
            lines.append(f"\n{d.icon} [{d.severity}] {d.title}")
            lines.append(f"   {d.description}")
            if d.evidence:
                lines.append(f"   Evidência: {d.evidence}")
            if d.causes:
                lines.append("   Causas possíveis:")
                for c in d.causes:
                    lines.append(f"     • {c}")
            if d.actions:
                lines.append("   Ações recomendadas:")
                for a in d.actions:
                    lines.append(f"     → {a}")

        lines.append("\n" + "=" * 60)
        lines.append(f"Amostras coletadas: {len(self.samples)}")
        lines.append("=" * 60)

        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
