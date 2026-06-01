"""UI Flet 0.85+ para Windows.

4 telas via NavigationBar:
  0 - Conexão     : seleciona porta COM, conecta ELM327
  1 - Diagnóstico : scan OBD2 padrão (modos 01/03/07/09)
  2 - Renault     : scan proprietário Mode 21/22/18 (Logan 2012)
  3 - Logger      : datalogger contínuo com exportação CSV
"""

import threading
import time
import uuid
from collections import deque
from pathlib import Path

import flet as ft
from flet import dropdown


def _win_clipboard(text: str) -> bool:
    """Copia texto para área de transferência via clip.exe (Windows built-in).
    Compatível com Flet 0.85 (que não tem page.set_clipboard).
    """
    try:
        import subprocess
        proc = subprocess.Popen(
            "clip",
            shell=True,
            stdin=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        proc.stdin.write(text.encode("utf-16-le"))
        proc.stdin.close()
        proc.wait(timeout=3)
        return proc.returncode == 0
    except Exception:
        return False

from ..diagnostics import DiagnosticsService, DiagnosticReport
from ..pids import PidRegistry
from ..protocol import Elm327Protocol
from ..pyren import RenaultScanner, RenaultReport, ENGINE_ECU, ALL_ECUS, get_ecu
from ..scheduler import LoggerScheduler
from ..storage import SqliteStorage, TelemetrySample
from ..transport.base import IObdTransport
from ..transport.mock import MockTransport
from ..transport.serial_windows import SerialTransport, list_com_ports
from ..transport.bluetooth_windows import BluetoothDirectTransport, list_paired_bt_devices
from ..analysis import AnalysisSession, FuelSample, Diagnosis, FUELS, ENGINES
from ..debug_log import get_logger, log_path
from ..analysis.csv_writer import CsvSessionWriter
from ..analysis.m21_snapshot import collect_m21_snapshot, format_m21_for_display

_log = get_logger(__name__)


def _storage_dir() -> Path:
    p = Path.home() / "ObdLoganData"
    p.mkdir(parents=True, exist_ok=True)
    return p



class AppState:
    def __init__(self):
        self.transport: IObdTransport | None = None
        self.protocol: Elm327Protocol | None = None
        self.registry: PidRegistry | None = None
        self.scheduler: LoggerScheduler | None = None
        self.report: DiagnosticReport | None = None
        self.renault_report: RenaultReport | None = None
        self.session_id: str = ""
        self.selected_port: str | None = None
        self.storage: SqliteStorage | None = None

    def ensure_storage(self) -> SqliteStorage:
        if self.storage is None:
            self.storage = SqliteStorage(_storage_dir() / "obd_data.db")
        return self.storage



def _b(width, color):
    """Cria Border com 4 lados iguais (ft.border.all nao existe no Flet 0.85)."""
    side = ft.border.BorderSide(width, color)
    return ft.border.Border(left=side, top=side, right=side, bottom=side)
def main(page: ft.Page):
    page.title = "OBD2 Logan Scanner — Windows"
    page.theme_mode = ft.ThemeMode.DARK
    page.padding = 0

    # ── UI update dispatcher ──────────────────────────────────────────────────
    # Causa raiz: FletSocketServer.send_message() usa asyncio.Queue.put_nowait()
    # que não é thread-safe. Chamado de uma thread externa, o item é enfileirado
    # mas o event loop NÃO é despertado → a mensagem só chega ao Flutter quando
    # o loop acorda por outro motivo (ex: troca de aba dispara um evento de I/O).
    #
    # Solução correta: page.run_task() usa asyncio.run_coroutine_threadsafe()
    # + call_soon_threadsafe() que DESPERTA o loop. A coroutine roda DENTRO do
    # event loop → put_nowait() notifica o send_loop corretamente.
    #
    # _ui_pending evita acúmulo: se já há um update agendado no loop, novas
    # chamadas retornam imediato (o update pendente pega o estado mais recente).
    _ui_pending = threading.Lock()

    def _safe_page_update() -> None:
        """Agenda page.update() no event loop do Flet. Seguro de qualquer thread."""
        if not _ui_pending.acquire(blocking=False):
            return

        async def _do() -> None:
            try:
                page.update()
            except Exception as _e:
                _log.debug("_safe_page_update: %s", _e)
            finally:
                _ui_pending.release()

        try:
            page.run_task(_do)
        except Exception as _e:
            _ui_pending.release()
            _log.debug("_safe_page_update run_task: %s", _e)

    def _on_page_close(e):
        pass

    page.on_close = _on_page_close

    def _on_page_resize(_e=None):
        try:
            _CanvasChart.set_page_width(page.width or 800)
        except NameError:
            pass  # class not yet defined at first call
        _safe_page_update()

    page.on_resize = _on_page_resize

    state = AppState()
    state.registry = PidRegistry(
        Path(__file__).resolve().parents[1] / "config" / "pids.yaml"
    )

    # =====================================================================
    # TELA 0 — CONEXÃO  (modo BT direto + modo COM port)
    # =====================================================================

    # --- controles compartilhados ---
    conn_status = ft.Text("Desconectado", color=ft.Colors.RED_400, size=13)
    storage_info = ft.Text(
        f"Dados em: {_storage_dir()}", size=11, color=ft.Colors.GREY_500,
    )
    connect_btn = ft.ElevatedButton(
        "Conectar", icon=ft.Icons.BLUETOOTH_CONNECTED,
        bgcolor=ft.Colors.BLUE_700, color=ft.Colors.WHITE,
    )
    disconnect_btn = ft.ElevatedButton(
        "Desconectar", icon=ft.Icons.LINK_OFF,
        bgcolor=ft.Colors.RED_900, color=ft.Colors.WHITE, disabled=True,
    )

    def _on_connected_ok(label: str):
        _log.info("UI: conexão estabelecida — %s", label)
        conn_status.value = f"Conectado | {label}"
        conn_status.color = ft.Colors.GREEN_400
        scan_btn.disabled = False
        renault_scan_btn.disabled = False
        log_start_btn.disabled = False
        analysis_start_btn.disabled = False
        disconnect_btn.disabled = False
        connect_btn.disabled = True
        _safe_page_update()

    def _on_connect_fail(msg: str):
        _log.error("UI: falha na conexão — %s", msg)
        conn_status.value = msg
        conn_status.color = ft.Colors.RED_400
        connect_btn.disabled = False
        _safe_page_update()

    def _do_connect(transport: IObdTransport):
        """Compartilhado: inicializa ELM327 após transport.connect()."""
        _log.info("UI: iniciando _do_connect com %s", type(transport).__name__)
        try:
            if state.transport and state.transport.is_connected():
                _log.debug("UI: desconectando transporte anterior")
                state.transport.disconnect()
            state.transport = transport
            state.transport.connect()
            state.protocol = Elm327Protocol(state.transport)
            ok, msg = state.protocol.initialize()
            if ok:
                _on_connected_ok(msg)
            else:
                _on_connect_fail(f"Init ELM327 falhou: {msg}")
        except Exception as e:
            _log.exception("UI: exceção em _do_connect")
            _on_connect_fail(f"Erro: {e}")

    def _on_disconnect(_):
        _log.info("UI: desconectando")
        if state.transport:
            state.transport.disconnect()
        conn_status.value = "Desconectado"
        conn_status.color = ft.Colors.RED_400
        connect_btn.disabled = False
        disconnect_btn.disabled = True
        scan_btn.disabled = True
        renault_scan_btn.disabled = True
        log_start_btn.disabled = True
        analysis_start_btn.disabled = True
        _safe_page_update()

    disconnect_btn.on_click = _on_disconnect

    # ------------------------------------------------------------------
    # PAINEL A — Bluetooth Direto (sem porta COM)
    # ------------------------------------------------------------------
    bt_dd = ft.Dropdown(
        label="Dispositivo Bluetooth pareado",
        width=340, options=[],
        hint_text="Clique em Atualizar",
    )
    bt_refresh_btn = ft.IconButton(icon=ft.Icons.REFRESH, tooltip="Atualizar lista BT")
    bt_mac_field = ft.TextField(
        label="MAC manual (se não aparecer na lista)",
        hint_text="00:1D:A5:XX:XX:XX",
        width=240, max_length=17,
    )

    def _populate_bt_devices():
        devs = list_paired_bt_devices()
        # Dispositivos OBD/ELM aparecem primeiro na lista
        _obd_kw = ("obd", "elm", "obdii", "obd2", "vlink", "konnwei", "bluedriver", "icar")
        devs.sort(key=lambda d: 0 if any(k in d["name"].lower() for k in _obd_kw) else 1)

        bt_dd.options = [
            dropdown.Option(key=d["mac"], text=f"{d['name']}  ({d['mac']})")
            for d in devs
        ]
        if devs:
            first = devs[0]
            bt_dd.value    = first["mac"]
            bt_mac_field.value = first["mac"]   # sync inicial obrigatória
        else:
            bt_dd.hint_text = "Nenhum dispositivo pareado encontrado"
        _safe_page_update()

    def _on_bt_dd_change(e):
        """Mantém MAC field em sincronia com o dropdown."""
        if bt_dd.value:
            bt_mac_field.value = bt_dd.value
            _safe_page_update()

    bt_dd.on_change = _on_bt_dd_change
    bt_refresh_btn.on_click = lambda _: _populate_bt_devices()

    def _on_bt_connect(_):
        # Dropdown tem prioridade; campo de texto serve para entrada manual
        mac = (bt_dd.value or bt_mac_field.value or "").strip()
        _log.info("UI BT connect: mac_dd=%r  mac_field=%r  → usando=%r",
                  bt_dd.value, bt_mac_field.value, mac)
        if not mac:
            conn_status.value = "Selecione o dispositivo OBDII na lista acima"
            conn_status.color = ft.Colors.ORANGE_400
            _safe_page_update()
            return
        # Sincroniza o campo visual
        bt_mac_field.value = mac
        conn_status.value = f"Conectando a {mac}…  (aguarde até 20s)"
        conn_status.color = ft.Colors.YELLOW_400
        connect_btn.disabled = True
        _safe_page_update()
        threading.Thread(
            target=_do_connect,
            args=(BluetoothDirectTransport(mac),),
            daemon=True,
        ).start()

    # ------------------------------------------------------------------
    # PAINEL B — Porta COM (USB ou BT com COM atribuída)
    # ------------------------------------------------------------------
    com_dd = ft.Dropdown(label="Porta COM", width=260, options=[])
    com_refresh_btn = ft.IconButton(icon=ft.Icons.REFRESH, tooltip="Atualizar portas COM")

    def _populate_com_ports():
        devs = list_com_ports()
        com_dd.options = [
            dropdown.Option(
                key=d["port"],
                text=f"{d['port']}  {d['description']}" + (" [BT]" if d["is_bluetooth"] else ""),
            )
            for d in devs
        ]
        if devs:
            com_dd.value = devs[0]["port"]
        _safe_page_update()

    com_refresh_btn.on_click = lambda _: _populate_com_ports()

    def _on_com_connect(_):
        port = com_dd.value
        _log.info("UI COM connect: porta=%r", port)
        if not port:
            conn_status.value = "Selecione uma porta COM"
            conn_status.color = ft.Colors.ORANGE_400
            _safe_page_update()
            return
        conn_status.value = f"Conectando em {port}…"
        conn_status.color = ft.Colors.YELLOW_400
        connect_btn.disabled = True
        _safe_page_update()
        transport = MockTransport() if port == "MOCK" else SerialTransport(port)
        threading.Thread(target=_do_connect, args=(transport,), daemon=True).start()

    # ------------------------------------------------------------------
    # Tab switcher BT / COM
    # ------------------------------------------------------------------
    _mode = {"bt": True}  # True = Bluetooth direto

    panel_bt = ft.Column([
        ft.Container(
            content=ft.Text(
                "⚠  O leitor OBD precisa estar PLUGADO na tomada OBD do carro\n"
                "    para ter energia e aceitar conexão Bluetooth.",
                size=12, color=ft.Colors.AMBER_300,
            ),
            padding=8,
            border=_b(1, ft.Colors.AMBER_800),
            border_radius=6,
        ),
        ft.Text(
            "Depois de plugar: aguarde 5s, selecione o dispositivo abaixo e clique Conectar.",
            size=12, color=ft.Colors.GREY_400,
        ),
        ft.Row([bt_dd, bt_refresh_btn]),
        bt_mac_field,
    ], spacing=8)

    panel_com = ft.Column([
        ft.Text(
            "Para Bluetooth: vá em Configurações → Bluetooth → Mais opções Bluetooth\n"
            "→ Portas COM → anote a porta Outgoing e selecione abaixo.\n"
            "Para USB ELM327: instale o driver CH340/FTDI.",
            size=12, color=ft.Colors.GREY_400,
        ),
        ft.Row([com_dd, com_refresh_btn]),
    ], spacing=8)

    panel_bt_wrap  = ft.Container(panel_bt,  visible=True)
    panel_com_wrap = ft.Container(panel_com, visible=False)

    btn_mode_bt = ft.ElevatedButton(
        "Bluetooth Direto",
        icon=ft.Icons.BLUETOOTH,
        bgcolor=ft.Colors.BLUE_800,
        color=ft.Colors.WHITE,
    )
    btn_mode_com = ft.ElevatedButton(
        "Porta COM / USB",
        icon=ft.Icons.USB,
        bgcolor=ft.Colors.GREY_800,
        color=ft.Colors.WHITE,
    )

    def _select_bt(_=None):
        _mode["bt"] = True
        panel_bt_wrap.visible  = True
        panel_com_wrap.visible = False
        btn_mode_bt.bgcolor  = ft.Colors.BLUE_800
        btn_mode_com.bgcolor = ft.Colors.GREY_800
        connect_btn.icon    = ft.Icons.BLUETOOTH_CONNECTED
        connect_btn.on_click = _on_bt_connect
        _safe_page_update()

    def _select_com(_=None):
        _mode["bt"] = False
        panel_bt_wrap.visible  = False
        panel_com_wrap.visible = True
        btn_mode_bt.bgcolor  = ft.Colors.GREY_800
        btn_mode_com.bgcolor = ft.Colors.BLUE_800
        connect_btn.icon    = ft.Icons.USB
        connect_btn.on_click = _on_com_connect
        _safe_page_update()

    btn_mode_bt.on_click  = _select_bt
    btn_mode_com.on_click = _select_com
    connect_btn.on_click  = _on_bt_connect  # default

    view_connect = ft.Container(
        padding=15,
        content=ft.Column([
            ft.Text("Conexão ELM327", size=20, weight=ft.FontWeight.BOLD),
            ft.Row([btn_mode_bt, btn_mode_com], spacing=8),
            panel_bt_wrap,
            panel_com_wrap,
            ft.Divider(height=8),
            ft.Row([connect_btn, disconnect_btn]),
            conn_status,
            storage_info,
            ft.TextButton(
                f"📋 Log de debug: {log_path()}",
                style=ft.ButtonStyle(color=ft.Colors.GREY_600),
                on_click=lambda _: __import__("subprocess").Popen(
                    ["notepad.exe", str(log_path())]
                ),
            ),
        ]),
    )

    # =====================================================================
    # TELA 1 — DIAGNÓSTICO OBD2 PADRÃO
    # =====================================================================
    scan_btn = ft.ElevatedButton(
        "Scan OBD2 Completo", icon=ft.Icons.SEARCH, disabled=True,
    )
    scan_log = ft.ListView(expand=False, height=160, spacing=4)
    report_view = ft.Column([], scroll=ft.ScrollMode.AUTO, expand=True)

    def _log_scan(msg: str):
        scan_log.controls.append(
            ft.Text(f"• {msg}", size=12, color=ft.Colors.CYAN_200)
        )
        if len(scan_log.controls) > 50:
            scan_log.controls.pop(0)
        _safe_page_update()

    def _render_obd_report(rep: DiagnosticReport):
        report_view.controls.clear()

        def section(title, content):
            return ft.Container(
                content=ft.Column([
                    ft.Text(title, size=14, weight=ft.FontWeight.BOLD,
                            color=ft.Colors.AMBER_400),
                    content,
                ]),
                padding=10,
                border=_b(1, ft.Colors.GREY_700),
                border_radius=6,
                margin=ft.margin.Margin(0,0,0,8),
            )

        report_view.controls.append(section(
            "Identificação",
            ft.Column([
                ft.Text(f"Protocolo: {rep.protocol or '?'}"),
                ft.Text(f"VIN: {rep.vin or 'não obtido'}"),
                ft.Text(f"ELM: {rep.elm_version or '?'}"),
            ]),
        ))
        report_view.controls.append(section(
            f"PIDs suportados ({len(rep.supported_pids)})",
            ft.Text(", ".join(rep.supported_pids) or "(nenhum)",
                    size=11, color=ft.Colors.GREY_300),
        ))
        dtc_color = ft.Colors.RED_400 if rep.stored_dtcs else ft.Colors.GREEN_400
        report_view.controls.append(section(
            f"DTCs Armazenados ({len(rep.stored_dtcs)})",
            ft.Text(", ".join(rep.stored_dtcs) or "Nenhum", color=dtc_color),
        ))
        if rep.pending_dtcs:
            report_view.controls.append(section(
                f"DTCs Pendentes ({len(rep.pending_dtcs)})",
                ft.Text(", ".join(rep.pending_dtcs), color=ft.Colors.ORANGE_400),
            ))
        if rep.freeze_frame:
            report_view.controls.append(section(
                "Freeze Frame",
                ft.Text("\n".join(f"  {k}: {v}" for k, v in rep.freeze_frame.items()), size=11),
            ))

        rp = _storage_dir() / f"scan_obd2_{int(time.time())}.txt"
        with open(rp, "w", encoding="utf-8") as f:
            f.write(
                f"Protocolo: {rep.protocol}\nVIN: {rep.vin}\nELM: {rep.elm_version}\n\n"
                f"PIDs ({len(rep.supported_pids)}):\n{', '.join(rep.supported_pids)}\n\n"
                f"DTCs: {rep.stored_dtcs}\nPendentes: {rep.pending_dtcs}\nFreeze: {rep.freeze_frame}\n"
            )
        _log_scan(f"Relatório salvo: {rp}")

    def _on_scan(_):
        scan_log.controls.clear()
        report_view.controls.clear()
        _safe_page_update()

        def _do():
            try:
                service = DiagnosticsService(state.protocol)
                state.report = service.run_full_scan(progress_cb=_log_scan)
                _render_obd_report(state.report)
            except Exception as e:
                _log_scan(f"FALHA: {e}")
            _safe_page_update()

        threading.Thread(target=_do, daemon=True).start()

    scan_btn.on_click = _on_scan

    view_scan = ft.Container(
        padding=15,
        content=ft.Column([
            ft.Text("Diagnóstico OBD2 (Modos 01/03/07/09)", size=20, weight=ft.FontWeight.BOLD),
            scan_btn,
            ft.Text("Log:", size=12, weight=ft.FontWeight.BOLD),
            scan_log,
            ft.Text("Relatório:", size=12, weight=ft.FontWeight.BOLD),
            report_view,
        ], expand=True),
    )

    # =====================================================================
    # TELA 2 — SCAN RENAULT (Mode 21/22/18)
    # =====================================================================
    ecu_dd = ft.Dropdown(
        label="Módulo ECU",
        width=260,
        options=[dropdown.Option(key=e.short, text=e.name) for e in ALL_ECUS],
        value=ENGINE_ECU.short,
    )
    renault_scan_btn = ft.ElevatedButton(
        "Scan Renault (Mode 21)", icon=ft.Icons.CAR_REPAIR, disabled=True,
    )
    renault_log = ft.ListView(expand=False, height=160, spacing=4)
    renault_report_view = ft.Column([], scroll=ft.ScrollMode.AUTO, expand=True)

    def _log_renault(msg: str):
        renault_log.controls.append(
            ft.Text(f"• {msg}", size=12, color=ft.Colors.LIME_200)
        )
        if len(renault_log.controls) > 60:
            renault_log.controls.pop(0)
        _safe_page_update()

    def _render_renault_report(rep: RenaultReport):
        renault_report_view.controls.clear()

        def section(title, content, border_color=ft.Colors.GREY_700):
            return ft.Container(
                content=ft.Column([
                    ft.Text(title, size=14, weight=ft.FontWeight.BOLD,
                            color=ft.Colors.LIME_400),
                    content,
                ]),
                padding=10,
                border=_b(1, border_color),
                border_radius=6,
                margin=ft.margin.Margin(0, 0, 0, 8),
            )

        # Cabeçalho de sessão
        session_color = ft.Colors.GREEN_400 if rep.session_ok else ft.Colors.RED_400
        renault_report_view.controls.append(section(
            f"Sessão — {rep.protocol_used}",
            ft.Column([
                ft.Text(f"ECU: {rep.ecu}"),
                ft.Text(f"Sessão: {'OK' if rep.session_ok else 'FALHOU'}",
                        color=session_color),
                ft.Text(f"Versão ECU: {rep.ecu_version or 'não disponível'}"),
                ft.Text(f"Protocolo usado: {rep.protocol_used}",
                        color=ft.Colors.CYAN_300, size=11),
            ]),
            border_color=session_color,
        ))

        # Erros críticos de sessão
        if rep.errors:
            err_items = [ft.Text(f"• {e}", size=11, color=ft.Colors.RED_300)
                         for e in rep.errors]
            renault_report_view.controls.append(section(
                f"⚠ Erros ({len(rep.errors)})",
                ft.Column(err_items),
                border_color=ft.Colors.RED_700,
            ))

        # Parâmetros Mode 21
        if rep.live_data:
            ok_data      = [s for s in rep.live_data if s.status == "OK" and s.value is not None]
            neg_data     = [s for s in rep.live_data if s.status.startswith("NEG_RESP")]
            parse_data   = [s for s in rep.live_data if s.status == "PARSE_ERROR"]
            other_errors = [s for s in rep.live_data if s.status not in
                            ("OK", "NO_DATA") and not s.status.startswith("NEG_RESP")
                            and s.status != "PARSE_ERROR"]
            nd_data      = [s for s in rep.live_data if s.status == "NO_DATA"]
            all_errors   = neg_data + parse_data + other_errors

            items = []
            # OK — verde
            for s in ok_data:
                items.append(ft.Text(
                    f"✓ [21 {s.local_id:02X}] {s.description}: {s.value:.2f} {s.unit}",
                    size=12, color=ft.Colors.GREEN_300,
                ))
            # NEG_RESP — laranja (ECU recusou o serviço — provável limitação CAN)
            if neg_data:
                items.append(ft.Container(
                    content=ft.Column([
                        ft.Text(
                            f"⚠ {len(neg_data)} parâmetros recusados pelo ECU (NEG_RESP)",
                            size=12, color=ft.Colors.ORANGE_300, weight=ft.FontWeight.BOLD,
                        ),
                        ft.Text(
                            "O ECU respondeu 'service not supported' para esses IDs.\n"
                            "Isso pode indicar que a sessão estendida (10 03) não foi aceita,\n"
                            "ou que este ECU não suporta Mode 21 via CAN para esses parâmetros.",
                            size=11, color=ft.Colors.GREY_400,
                        ),
                        ft.Text(
                            "IDs recusados: " + ", ".join(f"21 {s.local_id:02X}" for s in neg_data),
                            size=11, color=ft.Colors.ORANGE_400,
                        ),
                    ], spacing=3),
                    padding=8, border=_b(1, ft.Colors.ORANGE_800), border_radius=4,
                    margin=ft.margin.Margin(0,4,0,4),
                ))
            # PARSE_ERROR — vermelho com raw para debug
            for s in parse_data:
                items.append(ft.Text(
                    f"✗ [21 {s.local_id:02X}] {s.description}: PARSE_ERROR",
                    size=12, color=ft.Colors.RED_400,
                ))
                if s.raw_response:
                    items.append(ft.Text(
                        f"   raw={s.raw_response[:50]}",
                        size=10, color=ft.Colors.GREY_600, italic=True,
                    ))
            # Outros erros
            for s in other_errors:
                items.append(ft.Text(
                    f"✗ [21 {s.local_id:02X}] {s.description}: {s.status}",
                    size=12, color=ft.Colors.RED_400,
                ))
            # NO_DATA — cinza
            for s in nd_data:
                items.append(ft.Text(
                    f"– [21 {s.local_id:02X}] {s.description}: sem dados",
                    size=12, color=ft.Colors.GREY_600,
                ))

            title_color = ft.Colors.GREEN_400 if not all_errors else (
                ft.Colors.ORANGE_400 if ok_data else ft.Colors.RED_400
            )
            renault_report_view.controls.append(section(
                f"Mode 21: {len(ok_data)} OK  |  {len(neg_data)} recusados  |  "
                f"{len(parse_data)} erro parse  |  {len(nd_data)} sem dado",
                ft.Column(items, spacing=2),
                border_color=title_color,
            ))

        # DTCs
        if rep.protocol_used != "CAN":
            dtc_color = ft.Colors.RED_400 if rep.renault_dtcs else ft.Colors.GREEN_400
            renault_report_view.controls.append(section(
                f"DTCs Renault (Mode 18) — {len(rep.renault_dtcs)}",
                ft.Text(", ".join(rep.renault_dtcs) or "Nenhum DTC Renault", color=dtc_color),
            ))
        else:
            renault_report_view.controls.append(section(
                "DTCs",
                ft.Text(
                    "Este carro usa CAN — use a aba OBD2 → Scan Completo para ver DTCs.",
                    size=11, color=ft.Colors.GREY_400,
                ),
            ))

        # Salva relatório
        rp = _storage_dir() / f"scan_renault_{int(time.time())}.txt"
        try:
            with open(rp, "w", encoding="utf-8") as f:
                f.write(f"ECU: {rep.ecu}\nProtocolo: {rep.protocol_used}\n"
                        f"Sessão OK: {rep.session_ok}\nVersão: {rep.ecu_version}\n\n")
                if rep.errors:
                    f.write("ERROS:\n" + "\n".join(f"  {e}" for e in rep.errors) + "\n\n")
                f.write("Parâmetros Mode 21:\n")
                for s in rep.live_data:
                    val = f"{s.value:.2f} {s.unit}" if s.value is not None else s.status
                    raw = f"  raw={s.raw_response[:40]!r}" if s.status not in ("OK","NO_DATA") else ""
                    f.write(f"  [21 {s.local_id:02X}] {s.description}: {val}{raw}\n")
                f.write(f"\nDTCs: {rep.renault_dtcs}\n")
                f.write(f"\nNotas:\n" + "\n".join(f"  {n}" for n in rep.notes))
        except Exception as e:
            _log.warning("Falha ao salvar relatório Renault: %s", e)
        _log_renault(f"Relatório salvo: {rp}")

    def _stop_all_background_ops():
        """Para logger e análise antes de operações exclusivas (ex: Renault scan)."""
        if state.scheduler and state.scheduler.is_running():
            _log.info("UI: parando logger para operação exclusiva")
            state.scheduler.stop()
            log_start_btn.disabled = False
            log_stop_btn.disabled  = True
        if _analysis_state.get("running"):
            _log.info("UI: parando análise para operação exclusiva")
            _analysis_state["running"] = False
            analysis_start_btn.disabled = False
            analysis_stop_btn.disabled  = True

    def _on_renault_scan(_):
        if state.scheduler and state.scheduler.is_running():
            _log_renault("⚠ Parando logger para executar scan Renault…")
            _stop_all_background_ops()

        renault_log.controls.clear()
        renault_report_view.controls.clear()
        renault_scan_btn.disabled = True
        _safe_page_update()

        ecu = get_ecu(ecu_dd.value or "engine") or ENGINE_ECU

        def _do():
            try:
                scanner = RenaultScanner(state.protocol)
                state.renault_report = scanner.full_scan(ecu=ecu, progress_cb=_log_renault)
                _render_renault_report(state.renault_report)

                # Re-inicializa ELM327 com ATZ completo após qualquer scan Renault
                _log_renault("Restaurando ELM327 (ATZ + reinit)…")
                _log.info("UI: reiniciando ELM327 após scan Renault")
                state.protocol.send("ATZ", timeout_s=3.0)
                time.sleep(0.8)
                ok, msg = state.protocol.initialize()
                if ok:
                    _log_renault(f"ELM327 restaurado: {msg}")
                else:
                    _log_renault(f"⚠ ELM327 reinit falhou: {msg} — reconecte se necessário")
            except Exception as e:
                _log.exception("UI: exceção no Renault scan")
                _log_renault(f"FALHA: {e}")
            finally:
                renault_scan_btn.disabled = False
                try:
                    _safe_page_update()
                except RuntimeError:
                    pass

        threading.Thread(target=_do, daemon=True).start()

    renault_scan_btn.on_click = _on_renault_scan

    def _copy_renault_log(_):
        """Copia log do scan Renault para área de transferência."""
        try:
            lines = [c.value for c in renault_log.controls if hasattr(c, "value")]
            if state.renault_report:
                rep = state.renault_report
                lines.insert(0, f"=== SCAN RENAULT MODE 21 ===")
                lines.insert(1, f"ECU: {rep.ecu} | Protocolo: {rep.protocol_used}")
                lines.insert(2, f"Sessão OK: {rep.session_ok}")
                if rep.errors:
                    lines.append("ERROS:")
                    lines.extend(f"  {e}" for e in rep.errors)
                for s in rep.live_data:
                    val = f"{s.value:.2f} {s.unit}" if s.value is not None else s.status
                    raw = f"  raw={s.raw_response[:30]!r}" if s.status not in ("OK","NO_DATA") else ""
                    lines.append(f"[21 {s.local_id:02X}] {s.description}: {val}{raw}")
            text = "\n".join(lines)
            _win_clipboard(text)
            page.snack_bar = ft.SnackBar(
                ft.Text("✓ Log Renault copiado! Cole no chat do Claude.", size=13),
                bgcolor=ft.Colors.INDIGO_800, open=True,
            )
            _safe_page_update()
        except Exception as e:
            _log.warning("Falha ao copiar log Renault: %s", e)

    copy_renault_btn = ft.IconButton(
        icon=ft.Icons.CONTENT_COPY,
        tooltip="Copiar log para Claude",
        on_click=_copy_renault_log,
    )

    view_renault = ft.Container(
        padding=15,
        content=ft.Column([
            ft.Text("Diagnóstico Renault — Mode 21", size=20, weight=ft.FontWeight.BOLD),
            ft.Text(
                "Acessa parâmetros proprietários do ECU Renault via CAN/KWP2000.\n"
                "Logan 2012: UCE Motor (Sirius 32N/K7M) — protocolo CAN detectado automaticamente.",
                size=12, color=ft.Colors.GREY_400,
            ),
            ft.Row([ecu_dd, renault_scan_btn]),
            ft.Row([
                ft.Text("Log:", size=12, weight=ft.FontWeight.BOLD),
                copy_renault_btn,
            ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
            renault_log,
            ft.Text("Resultado:", size=12, weight=ft.FontWeight.BOLD),
            renault_report_view,
        ], expand=True),
    )

    # =====================================================================
    # TELA 3 — DATALOGGER
    # =====================================================================
    log_start_btn = ft.ElevatedButton(
        "Iniciar Logger", icon=ft.Icons.PLAY_ARROW,
        bgcolor=ft.Colors.GREEN_700, color=ft.Colors.WHITE, disabled=True,
    )
    log_stop_btn = ft.ElevatedButton(
        "Parar + Exportar CSV", icon=ft.Icons.STOP,
        bgcolor=ft.Colors.RED_700, color=ft.Colors.WHITE, disabled=True,
    )
    live_metrics = ft.Column([])
    pacing_text = ft.Text("Pacing: -- ms", size=12, color=ft.Colors.GREY_400)
    latency_text = ft.Text("Latência média: -- ms", size=12, color=ft.Colors.GREY_400)
    sample_count_text = ft.Text("Amostras: 0", size=12, color=ft.Colors.GREY_400)
    live_log = ft.ListView(expand=False, height=240, spacing=2)

    live_values: dict[str, ft.Text] = {}
    sample_counter = {"n": 0}



    def _on_sample(sample: TelemetrySample):
        sample_counter["n"] += 1
        sample_count_text.value = f"Amostras: {sample_counter['n']}"

        if sample.name not in live_values:
            t = ft.Text(f"{sample.name}: -- {sample.unit}", size=14)
            live_values[sample.name] = t
            live_metrics.controls.append(t)

        if sample.status == "SUCCESS" and sample.parsed_value is not None:
            live_values[sample.name].value = (
                f"{sample.name}: {sample.parsed_value:.2f} {sample.unit}"
            )
            live_values[sample.name].color = ft.Colors.GREEN_300
        else:
            live_values[sample.name].color = ft.Colors.ORANGE_400

        color = ft.Colors.GREEN_400 if sample.status == "SUCCESS" else ft.Colors.ORANGE_400
        live_log.controls.insert(0, ft.Text(
            f"[{sample.pid}] {sample.status} | {sample.transport_delay_ms:.0f}ms "
            f"| {sample.parsed_value} {sample.unit}",
            size=10, color=color,
        ))
        if len(live_log.controls) > 100:
            live_log.controls.pop()

        if state.scheduler:
            pacing_text.value = f"Pacing: {state.scheduler.current_pacing_ms:.0f} ms"
            latency_text.value = f"Latência média: {state.scheduler.avg_latency_ms:.0f} ms"
        _safe_page_update()

    def _on_error(msg: str):
        _log.warning("Scheduler error callback: %s", msg)
        live_log.controls.insert(0, ft.Text(f"ERRO: {msg}", color=ft.Colors.RED_400, size=11))
        _safe_page_update()

    def _on_log_start(_):
        state.session_id = f"sess_{int(time.time())}_{uuid.uuid4().hex[:6]}"
        sample_counter["n"] = 0
        storage = state.ensure_storage()
        state.scheduler = LoggerScheduler(state.protocol, state.registry, storage)
        if state.report and state.report.supported_pids:
            state.scheduler.set_supported_filter(state.report.supported_pids)
        metadata = {
            "vin": state.report.vin if state.report else None,
            "protocol": state.report.protocol if state.report else None,
        }
        state.scheduler.start(state.session_id, _on_sample, _on_error, metadata)
        log_start_btn.disabled = True
        log_stop_btn.disabled = False
        _safe_page_update()

    def _on_log_stop(_):
        if state.scheduler:
            state.scheduler.stop()
            storage = state.ensure_storage()
            csv_path = _storage_dir() / f"{state.session_id}.csv"
            rows = storage.export_csv(state.session_id, csv_path)
            stats = storage.session_stats(state.session_id)
            live_log.controls.insert(0, ft.Text(
                f"CSV: {csv_path} | {rows} linhas | "
                f"Confiab. {stats['reliability']*100:.1f}% | "
                f"Latência {stats['avg_delay_ms']:.0f}ms",
                color=ft.Colors.CYAN_300, size=12,
            ))
        log_start_btn.disabled = False
        log_stop_btn.disabled = True
        _safe_page_update()

    log_start_btn.on_click = _on_log_start
    log_stop_btn.on_click = _on_log_stop

    view_logger = ft.Container(
        padding=15,
        content=ft.Column([
            ft.Text("Datalogger OBD2", size=20, weight=ft.FontWeight.BOLD),
            ft.Row([log_start_btn, log_stop_btn], wrap=True),
            ft.Row([pacing_text, latency_text, sample_count_text], wrap=True),
            ft.Divider(),
            ft.Text("Métricas ao vivo:", size=12, weight=ft.FontWeight.BOLD),
            live_metrics,
            ft.Divider(),
            ft.Text("Log:", size=12, weight=ft.FontWeight.BOLD),
            live_log,
        ], expand=True, scroll=ft.ScrollMode.AUTO),
    )

    # =====================================================================
    # TELA 4 — ANÁLISE DE CONSUMO E DIAGNÓSTICO
    # =====================================================================
    _analysis_state: dict = {
        "session": None,
        "running": False,
        "thread": None,
        "chart_n": 0,
        "coolant_pts": deque(maxlen=120),
        "o2_b1s1_pts": deque(maxlen=120),
        "o2_b1s2_pts": deque(maxlen=120),
        "ltft_pts": deque(maxlen=120),
        "stft_pts": deque(maxlen=120),
        "timing_pts": deque(maxlen=120),
        # CSV + Mode 21
        "csv_writer": None,   # CsvSessionWriter
        "m21_data": {},       # último snapshot Mode 21
    }

    # Seletores
    fuel_dd = ft.Dropdown(
        label="Combustível",
        width=170,
        value="E25",
        options=[dropdown.Option(key=k, text=v["label"]) for k, v in FUELS.items()],
    )
    engine_dd = ft.Dropdown(
        label="Motor",
        width=170,
        value="D4F_1.0",
        options=[dropdown.Option(key=k, text=v["name"]) for k, v in ENGINES.items()],
    )

    # Botões
    analysis_start_btn = ft.ElevatedButton(
        "Iniciar Análise",
        icon=ft.Icons.DIRECTIONS_CAR,
        bgcolor=ft.Colors.GREEN_700,
        color=ft.Colors.WHITE,
        disabled=True,
    )
    analysis_stop_btn = ft.ElevatedButton(
        "Parar + Relatório",
        icon=ft.Icons.STOP,
        bgcolor=ft.Colors.RED_700,
        color=ft.Colors.WHITE,
        disabled=True,
    )
    m21_snap_btn = ft.ElevatedButton(
        "📡 Snapshot Mode 21",
        icon=ft.Icons.RADAR,
        bgcolor=ft.Colors.PURPLE_800,
        color=ft.Colors.WHITE,
        disabled=True,
        tooltip="Lê parâmetros proprietários Renault (injeção, timing, knock…)",
    )
    m21_status_text = ft.Text("Mode 21: —", size=11, color=ft.Colors.GREY_600, italic=True)
    csv_path_text   = ft.Text("", size=10, color=ft.Colors.GREY_600)

    # Painel de consumo instantâneo
    consumption_text = ft.Text("-- L/100km", size=32, weight=ft.FontWeight.BOLD,
                               color=ft.Colors.CYAN_300)
    kmL_text         = ft.Text("(-- km/L)", size=18, color=ft.Colors.CYAN_200)
    dist_text        = ft.Text("Distância: 0.0 km", size=12, color=ft.Colors.GREY_400)
    fuel_used_text   = ft.Text("Combustível: 0.0 L", size=12, color=ft.Colors.GREY_400)
    session_time_text = ft.Text("Tempo: 00:00", size=12, color=ft.Colors.GREY_400)

    # Parâmetros ao vivo
    live_params: dict[str, ft.Text] = {}
    live_params_col = ft.Column([], spacing=3)

    _PARAM_DEFS = [
        ("rpm",         "RPM",        "rpm"),
        ("speed_kmh",   "Velocidade", "km/h"),
        ("map_kpa",     "MAP",        "kPa"),
        ("engine_load", "Carga",      "%"),
        ("coolant_c",   "Temp. água", "°C"),
        ("iat_c",       "Temp. ar",   "°C"),
        ("stft",        "STFT",       "%"),
        ("ltft",        "LTFT",       "%"),
        ("o2_b1s1",     "O2 B1S1",   "V"),
        ("timing_adv",  "Avanço ign.","°"),
        ("bat_voltage", "Bateria",    "V"),
    ]

    for attr, label, unit in _PARAM_DEFS:
        t = ft.Text(f"{label}: --", size=13, color=ft.Colors.GREY_300)
        live_params[attr] = t
        live_params_col.controls.append(t)

    # Painel de alertas ao vivo
    alerts_col = ft.Column([], spacing=4)
    alerts_seen: set[str] = set()

    # Painel do relatório final
    report_col = ft.Column([], scroll=ft.ScrollMode.AUTO, expand=True)

    def _update_live_param(attr: str, value, unit: str, label: str):
        if value is None:
            live_params[attr].value = f"{label}: --"
            live_params[attr].color = ft.Colors.GREY_500
        else:
            live_params[attr].value = f"{label}: {value:.1f} {unit}"
            # Colorir parâmetros críticos
            if attr == "coolant_c":
                live_params[attr].color = (
                    ft.Colors.RED_400 if value < 75 else
                    ft.Colors.GREEN_300 if value >= 85 else ft.Colors.YELLOW_400
                )
            elif attr in ("stft", "ltft"):
                live_params[attr].color = (
                    ft.Colors.RED_400   if abs(value) > 8 else
                    ft.Colors.YELLOW_400 if abs(value) > 4 else ft.Colors.GREEN_300
                )
            elif attr == "o2_b1s1":
                live_params[attr].color = ft.Colors.CYAN_300
            elif attr == "bat_voltage":
                live_params[attr].color = ft.Colors.RED_400 if value < 12.5 else ft.Colors.GREEN_300
            else:
                live_params[attr].color = ft.Colors.GREY_300

    def _add_live_alert(d: Diagnosis):
        if d.code in alerts_seen:
            return
        alerts_seen.add(d.code)
        color = {
            "CRITICO": ft.Colors.RED_400,
            "AVISO":   ft.Colors.ORANGE_400,
            "INFO":    ft.Colors.BLUE_300,
        }.get(d.severity, ft.Colors.GREY_300)
        alerts_col.controls.insert(0, ft.Container(
            content=ft.Column([
                ft.Text(f"{d.icon} {d.title}", size=13, weight=ft.FontWeight.BOLD, color=color),
                ft.Text(d.description, size=11, color=ft.Colors.GREY_300),
            ], spacing=2),
            padding=8,
            border=_b(1, color),
            border_radius=6,
            margin=ft.margin.Margin(0,0,0,4),
        ))

    def _render_final_report(session: AnalysisSession):
        report_col.controls.clear()
        issues = session.finalize()

        # Cabeçalho resumo
        L100 = session.avg_consumption_L100
        L100_str = f"{L100:.1f} L/100km  ({100/L100:.1f} km/L)" if L100 else "dados insuficientes"
        report_col.controls.append(ft.Container(
            content=ft.Column([
                ft.Text("RESUMO DA SESSÃO", size=14, weight=ft.FontWeight.BOLD,
                        color=ft.Colors.CYAN_400),
                ft.Text(f"Distância: {session.distance_km:.2f} km", size=12),
                ft.Text(f"Combustível estimado: {session.fuel_used_L:.2f} L", size=12),
                ft.Text(f"Consumo médio: {L100_str}", size=13, weight=ft.FontWeight.BOLD),
                ft.Text(f"Duração: {session.duration_s/60:.1f} min", size=12),
                ft.Text(f"Vel. média: {session.avg_speed_kmh or 0:.0f} km/h", size=12),
            ], spacing=4),
            padding=12,
            border=_b(1, ft.Colors.CYAN_800),
            border_radius=8,
            margin=ft.margin.Margin(0,0,0,10),
        ))

        # Diagnósticos
        for d in issues:
            color = {
                "CRITICO": ft.Colors.RED_400,
                "AVISO":   ft.Colors.ORANGE_400,
                "INFO":    ft.Colors.GREEN_400,
            }.get(d.severity, ft.Colors.GREY_300)

            items = []
            if d.description:
                items.append(ft.Text(d.description, size=12, color=ft.Colors.GREY_300))
            if d.evidence:
                items.append(ft.Text(f"Evidência: {d.evidence}", size=11,
                                     color=ft.Colors.GREY_500, italic=True))
            if d.causes:
                items.append(ft.Text("Causas possíveis:", size=12,
                                     weight=ft.FontWeight.BOLD, color=ft.Colors.GREY_400))
                for c in d.causes:
                    items.append(ft.Text(f"  • {c}", size=11, color=ft.Colors.GREY_300))
            if d.actions:
                items.append(ft.Text("Ações recomendadas:", size=12,
                                     weight=ft.FontWeight.BOLD, color=ft.Colors.GREEN_400))
                for a in d.actions:
                    items.append(ft.Text(f"  → {a}", size=11, color=ft.Colors.GREEN_300))

            report_col.controls.append(ft.Container(
                content=ft.Column([
                    ft.Text(f"{d.icon}  {d.title}  [{d.severity}]", size=13,
                            weight=ft.FontWeight.BOLD, color=color),
                    *items,
                ], spacing=3),
                padding=10,
                border=_b(1, color),
                border_radius=6,
                margin=ft.margin.Margin(0,0,0,8),
            ))

        # Exporta arquivo
        rp = _storage_dir() / f"analise_{int(time.time())}.txt"
        session.export_txt(str(rp))
        _analysis_state["last_report_path"] = str(rp)

        def _copy_to_clipboard(_):
            """Copia o relatório formatado para área de transferência."""
            try:
                with open(rp, encoding="utf-8") as f:
                    txt = f.read()
                # Acrescenta info de DTCs se disponível
                extra = ""
                if state.report and state.report.dtcs:
                    extra = f"\nDTCs: {', '.join(state.report.dtcs)}"
                if state.report and state.report.pending_dtcs:
                    extra += f"\nPendentes: {', '.join(state.report.pending_dtcs)}"
                _win_clipboard(txt + extra)
                page.snack_bar = ft.SnackBar(
                    ft.Text("✓ Relatório copiado! Cole no chat do Claude.", size=13),
                    bgcolor=ft.Colors.GREEN_800, open=True,
                )
                _safe_page_update()
            except Exception as e:
                _log.warning("Falha ao copiar relatório: %s", e)

        def _open_report_file(_):
            import subprocess
            subprocess.Popen(["notepad.exe", str(rp)])

        report_col.controls.append(ft.Row([
            ft.ElevatedButton(
                "📋 Copiar para Claude",
                icon=ft.Icons.CONTENT_COPY,
                bgcolor=ft.Colors.INDIGO_700,
                color=ft.Colors.WHITE,
                on_click=_copy_to_clipboard,
            ),
            ft.ElevatedButton(
                "📄 Abrir Arquivo",
                icon=ft.Icons.OPEN_IN_NEW,
                bgcolor=ft.Colors.GREY_800,
                color=ft.Colors.WHITE,
                on_click=_open_report_file,
            ),
        ], spacing=8, wrap=True))

    # ── Loop de coleta de dados da análise ──────────────────────────────

    def _analysis_loop(session: AnalysisSession):
        """Coleta dados OBD2 e alimenta a sessão de análise."""
        from ..pids.registry import extract_payload

        # ── PIDs divididos por prioridade ────────────────────────────────────
        # Rápidos (coletados todo ciclo, ~400ms cada): consumo + display imediato
        _PIDS_FAST = [
            ("010C", "rpm"),
            ("010D", "speed_kmh"),
            ("010B", "map_kpa"),
            ("0105", "coolant_c"),
            ("0106", "stft"),
            ("0107", "ltft"),
        ]
        # Médios (a cada 2 ciclos): dinâmica do motor
        _PIDS_MED = [
            ("010F", "iat_c"),
            ("010E", "timing_adv"),
            ("0104", "engine_load"),
            ("0111", "throttle"),
        ]
        # Lentos (a cada 4 ciclos): sensores auxiliares
        _PIDS_SLOW = [
            ("0114", "o2_b1s1"),
            ("0115", "o2_b1s2"),
        ]
        _bat_fails = 0

        _loop_n = 0

        def _read_pid(pid: str, attr: str, timeout: float = 1.5) -> bool:
            """Lê um PID, atualiza sample e display imediatamente. Retorna True se leu."""
            nonlocal _bat_fails
            if not state.protocol or not _analysis_state["running"]:
                return False
            pid_def = state.registry.get(pid)
            if not pid_def:
                return False
            r = state.protocol.send(pid, timeout_s=timeout)
            if r.status.value == "SUCCESS" and r.cleaned:
                try:
                    payload = extract_payload(r.cleaned, pid_def)
                    if payload:
                        value = pid_def.decoder(payload)
                        if value is not None:
                            setattr(sample, attr, value)
                            # Atualiza label imediatamente
                            for a, label, unit in _PARAM_DEFS:
                                if a == attr:
                                    _update_live_param(a, value, unit, label)
                                    break
                            if pid == "0142":
                                _bat_fails = 0
                            return True
                except Exception:
                    pass
            if pid == "0142":
                _bat_fails += 1
            return False

        while _analysis_state["running"]:
            sample = FuelSample(timestamp=time.time())
            any_data = False

            # ── 1) PIDs rápidos (sempre) — timeout curto, UI atualiza após cada um ──
            for pid, attr in _PIDS_FAST:
                if _read_pid(pid, attr, timeout=1.0):
                    any_data = True
                _safe_page_update()  # atualiza UI imediatamente após cada PID

            # ── 2) PIDs médios (a cada 2 ciclos) ─────────────────────────────
            if _loop_n % 2 == 0:
                for pid, attr in _PIDS_MED:
                    if _read_pid(pid, attr, timeout=1.0):
                        any_data = True
                    _safe_page_update()

            # ── 3) PIDs lentos (a cada 4 ciclos) ─────────────────────────────
            if _loop_n % 4 == 0:
                for pid, attr in _PIDS_SLOW:
                    if _read_pid(pid, attr, timeout=1.5):
                        any_data = True

            # ── 4) Bateria (a cada 8 ciclos, para se falhar demais) ───────────
            if _loop_n % 8 == 0 and _bat_fails < 5:
                _read_pid("0142", "bat_voltage", timeout=1.0)

            _loop_n += 1

            if any_data:
                alerts = session.ingest(sample)

                # ── Salva sample no CSV (sempre, em tempo real) ───────────────
                _csv = _analysis_state.get("csv_writer")
                if _csv:
                    try:
                        # Atualiza dados M21 no writer se houver snapshot novo
                        m21 = _analysis_state.get("m21_data", {})
                        if m21:
                            _csv.update_m21(m21)
                        _csv.write(sample, session.distance_km, session.fuel_used_L)
                    except Exception as _e:
                        _log.debug("CSV write error: %s", _e)

                # ── Consumo: mostra MÉDIA DA SESSÃO (não instantâneo) ─────────
                avg_l100 = session.avg_consumption_L100
                if avg_l100 and session.distance_km > 0.1:
                    # Média acumulada é o número correto
                    consumption_text.value = f"{avg_l100:.1f} L/100km"
                    kmL_text.value = f"({100/avg_l100:.1f} km/L)"
                    consumption_text.color = (
                        ft.Colors.RED_400    if avg_l100 > 13 else
                        ft.Colors.YELLOW_400 if avg_l100 > 9  else ft.Colors.GREEN_400
                    )
                elif sample.instant_L100 and sample.instant_L100 < 50:
                    # Ainda sem distância suficiente → mostra instantâneo com aviso
                    l100 = sample.instant_L100
                    consumption_text.value = f"~{l100:.1f} L/100km"
                    kmL_text.value = "(estimativa)"
                    consumption_text.color = ft.Colors.GREY_400
                else:
                    consumption_text.value = "-- L/100km"
                    kmL_text.value = "(parado)" if (sample.speed_kmh or 0) < 3 else "(calculando)"

                dist_text.value      = f"Distância: {session.distance_km:.2f} km"
                fuel_used_text.value = f"Combustível: {session.fuel_used_L:.3f} L"
                elapsed = int(session.duration_s)
                session_time_text.value = f"Tempo: {elapsed//60:02d}:{elapsed%60:02d}"

                for diag in alerts:
                    _add_live_alert(diag)

                # ── Charts update ────────────────────────────────────────
                _cn = _analysis_state["chart_n"]
                _analysis_state["chart_n"] = _cn + 1
                _x = _cn
                _x_min = max(0, _x - 119)
                _x_max = max(120, _x + 1)

                if sample.coolant_c is not None:
                    _pts = _analysis_state["coolant_pts"]
                    _pts.append((_x, max(20.0, min(110.0, sample.coolant_c))))
                    _cs_coolant.points = list(_pts)
                    _cv_coolant.value = f"Temp: {sample.coolant_c:.0f}°C"
                    _cv_coolant.color = (
                        ft.Colors.ORANGE_400 if sample.coolant_c < 75 else
                        ft.Colors.GREEN_400 if sample.coolant_c < 100 else ft.Colors.RED_400
                    )

                if sample.o2_b1s1 is not None:
                    _pts = _analysis_state["o2_b1s1_pts"]
                    _pts.append((_x, max(0.0, min(1.05, sample.o2_b1s1))))
                    _cs_o2_b1s1.points = list(_pts)

                _o2_b2 = getattr(sample, "o2_b1s2", None)
                if _o2_b2 is not None:
                    _pts = _analysis_state["o2_b1s2_pts"]
                    _pts.append((_x, max(0.0, min(1.05, _o2_b2))))
                    _cs_o2_b1s2.points = list(_pts)

                if sample.o2_b1s1 is not None:
                    _b2_str = f" | B2:{_o2_b2:.2f}V" if _o2_b2 is not None else ""
                    _cv_o2.value = f"B1S1:{sample.o2_b1s1:.2f}V{_b2_str}"

                if sample.ltft is not None:
                    _pts = _analysis_state["ltft_pts"]
                    _pts.append((_x, max(-22.0, min(22.0, sample.ltft))))
                    _cs_ltft.points = list(_pts)
                if sample.stft is not None:
                    _pts = _analysis_state["stft_pts"]
                    _pts.append((_x, max(-22.0, min(22.0, sample.stft))))
                    _cs_stft.points = list(_pts)
                if sample.ltft is not None:
                    _s = sample.stft or 0.0
                    _cv_trim.value = f"LTFT:{sample.ltft:+.1f}% STFT:{_s:+.1f}%"
                    _total = abs(sample.ltft) + abs(_s)
                    _cv_trim.color = (
                        ft.Colors.RED_400 if _total > 15 else
                        ft.Colors.ORANGE_400 if _total > 7 else ft.Colors.GREEN_400
                    )

                if sample.timing_adv is not None:
                    _pts = _analysis_state["timing_pts"]
                    _pts.append((_x, max(-15.0, min(42.0, sample.timing_adv))))
                    _cs_timing.points = list(_pts)
                    _cv_timing.value = f"Avanço: {sample.timing_adv:+.1f}°"
                    _cv_timing.color = (
                        ft.Colors.RED_400 if sample.timing_adv < 0 else
                        ft.Colors.ORANGE_400 if sample.timing_adv < 8 else ft.Colors.GREEN_400
                    )

                # Redraw all canvases
                chart_coolant.redraw(_x_min, _x_max)
                chart_o2.redraw(_x_min, _x_max)
                chart_trim.redraw(_x_min, _x_max)
                chart_timing.redraw(_x_min, _x_max)

                # Health indicators (every 8 samples)
                if _cn % 8 == 0:
                    _c_pts = _cs_coolant.points
                    if _c_pts:
                        _mc = max(p[1] for p in _c_pts)
                        _rt = _cn / 5.0
                        if _rt > 3 and _mc < 78:
                            _h_thermo.value = f"🌡 Termostato: ⚠ {_mc:.0f}°C — preso aberto?"
                            _h_thermo.color = ft.Colors.RED_400
                        elif _mc >= 85:
                            _h_thermo.value = f"🌡 Termostato: ✓ {_mc:.0f}°C"
                            _h_thermo.color = ft.Colors.GREEN_400
                        else:
                            _h_thermo.value = f"🌡 Termostato: {_mc:.0f}°C (aquecendo)"
                            _h_thermo.color = ft.Colors.ORANGE_400

                    _o2_pts = [p[1] for p in _cs_o2_b1s1.points[-30:]]
                    if len(_o2_pts) >= 8:
                        _amp = max(_o2_pts) - min(_o2_pts)
                        if _amp > 0.5:
                            _h_lambda.value = f"🔵 Lambda B1S1: ✓ osc={_amp:.2f}V"
                            _h_lambda.color = ft.Colors.GREEN_400
                        elif _amp > 0.2:
                            _h_lambda.value = f"🔵 Lambda B1S1: ⚠ osc={_amp:.2f}V (lenta)"
                            _h_lambda.color = ft.Colors.ORANGE_400
                        else:
                            _h_lambda.value = f"🔵 Lambda B1S1: ✗ osc={_amp:.2f}V (falha?)"
                            _h_lambda.color = ft.Colors.RED_400

                        _o2b2p = [p[1] for p in _cs_o2_b1s2.points[-30:]]
                        if len(_o2b2p) >= 8 and _amp > 0.1:
                            _amp2 = max(_o2b2p) - min(_o2b2p)
                            _ratio = _amp2 / _amp
                            if _ratio < 0.3:
                                _h_cat.value = f"🟤 Catalisador: ✓ ratio={_ratio:.2f}"
                                _h_cat.color = ft.Colors.GREEN_400
                            elif _ratio < 0.6:
                                _h_cat.value = f"🟤 Catalisador: ⚠ degradando ({_ratio:.2f})"
                                _h_cat.color = ft.Colors.ORANGE_400
                            else:
                                _h_cat.value = f"🟤 Catalisador: ✗ falha? ({_ratio:.2f})"
                                _h_cat.color = ft.Colors.RED_400

                    _ltft_pts = [p[1] for p in _cs_ltft.points[-20:]]
                    if _ltft_pts:
                        _al = sum(_ltft_pts) / len(_ltft_pts)
                        if abs(_al) < 5:
                            _h_fuel.value = f"⛽ Combustível: ✓ LTFT={_al:+.1f}%"
                            _h_fuel.color = ft.Colors.GREEN_400
                        elif abs(_al) < 10:
                            _h_fuel.value = f"⛽ Combustível: ⚠ LTFT={_al:+.1f}%"
                            _h_fuel.color = ft.Colors.ORANGE_400
                        else:
                            _h_fuel.value = f"⛽ Combustível: ✗ LTFT={_al:+.1f}%"
                            _h_fuel.color = ft.Colors.RED_400

                    _t_pts = [p[1] for p in _cs_timing.points[-20:]]
                    if _t_pts:
                        _at = sum(_t_pts) / len(_t_pts)
                        if _at >= 10:
                            _h_timing.value = f"⚡ Ignição: ✓ {_at:+.0f}°"
                            _h_timing.color = ft.Colors.GREEN_400
                        elif _at >= 0:
                            _h_timing.value = f"⚡ Ignição: ⚠ {_at:+.0f}° (baixo)"
                            _h_timing.color = ft.Colors.ORANGE_400
                        else:
                            _h_timing.value = f"⚡ Ignição: ✗ {_at:+.0f}° retardado!"
                            _h_timing.color = ft.Colors.RED_400

                _safe_page_update()

            time.sleep(0.2)  # ~5 amostras/s (cada PID leva ~100ms por CAN)

    def _on_analysis_start(_):
        if not state.protocol:
            return
        alerts_seen.clear()
        alerts_col.controls.clear()
        report_col.controls.clear()
        _analysis_state["chart_n"] = 0
        for _k in ("coolant_pts","o2_b1s1_pts","o2_b1s2_pts","ltft_pts","stft_pts","timing_pts"):
            _analysis_state[_k].clear()
        for _s in (_cs_coolant, _cs_o2_b1s1, _cs_o2_b1s2, _cs_ltft, _cs_stft, _cs_timing):
            _s.points = []
        for _ch in (chart_coolant, chart_o2, chart_trim, chart_timing):
            _ch.redraw(0, 120)
        for _h in (_h_thermo, _h_lambda, _h_cat, _h_fuel, _h_timing):
            _h.value = _h.value.split(":")[0] + ": aguardando..."
            _h.color = ft.Colors.GREY_500
        consumption_text.value = "-- L/100km"
        kmL_text.value = "iniciando..."
        dist_text.value = "Distância: 0.0 km"
        fuel_used_text.value = "Combustível: 0.0 L"
        session_time_text.value = "Tempo: 00:00"
        for attr, label, unit in _PARAM_DEFS:
            live_params[attr].value = f"{label}: --"
            live_params[attr].color = ft.Colors.GREY_500
        _safe_page_update()

        fuel_type  = fuel_dd.value or "E25"
        engine_key = engine_dd.value or "D4F_1.0"
        session = AnalysisSession(fuel_type=fuel_type, engine=engine_key)
        _analysis_state["session"] = session
        _analysis_state["running"] = True
        _analysis_state["m21_data"] = {}

        # ── Cria CSV de dados brutos ──────────────────────────────────────
        import uuid as _uuid_mod
        _csv_name = f"raw_{int(time.time())}_{_uuid_mod.uuid4().hex[:6]}.csv"
        _csv_path = str(_storage_dir() / _csv_name)
        _csv_info = {
            "session_id": _csv_name,
            "engine": engine_key,
            "fuel": fuel_type,
            "protocol": getattr(state.report, "protocol", "N/A") if state.report else "N/A",
            "vin": getattr(state.report, "vin", "N/A") if state.report else "N/A",
        }
        _analysis_state["csv_writer"] = CsvSessionWriter(_csv_path, _csv_info)
        csv_path_text.value = f"📄 CSV: {_csv_name}"
        csv_path_text.color = ft.Colors.GREY_400

        t = threading.Thread(target=_analysis_loop, args=(session,), daemon=True)
        _analysis_state["thread"] = t
        t.start()

        analysis_start_btn.disabled = True
        analysis_stop_btn.disabled  = False
        m21_snap_btn.disabled       = False
        log_start_btn.disabled      = True
        _safe_page_update()

    def _on_analysis_stop(_):
        _analysis_state["running"] = False

        # Fecha o CSV
        _csv = _analysis_state.pop("csv_writer", None)
        if _csv:
            _csv.close()
            csv_path_text.value = f"📄 CSV salvo: {Path(_csv.path).name}"
            csv_path_text.color = ft.Colors.GREEN_400

        session: AnalysisSession = _analysis_state.get("session")
        if session:
            _render_final_report(session)

        analysis_start_btn.disabled = False
        analysis_stop_btn.disabled  = True
        m21_snap_btn.disabled       = True
        log_start_btn.disabled      = False
        _safe_page_update()

    def _on_m21_snapshot(_):
        """Coleta snapshot Mode 21 Renault em background durante análise."""
        if not state.protocol or not _analysis_state.get("running"):
            return
        m21_snap_btn.disabled = True
        m21_status_text.value = "🔄 Coletando Mode 21…"
        m21_status_text.color = ft.Colors.CYAN_400
        _safe_page_update()

        def _do_snap():
            log_lines = []
            m21 = collect_m21_snapshot(
                state.protocol,
                quick=False,
                progress_cb=lambda msg: log_lines.append(msg),
            )
            _analysis_state["m21_data"] = m21

            # Atualiza CSV writer com novos dados M21
            _csv = _analysis_state.get("csv_writer")
            if _csv and m21:
                _csv.update_m21(m21)

            # Exibe no UI
            lines = format_m21_for_display(m21)
            if lines:
                m21_status_text.value = "✓ Mode 21: " + " | ".join(lines[:3])
                m21_status_text.color = ft.Colors.GREEN_400
            else:
                m21_status_text.value = "⚠ Mode 21: sem dados (ECU não respondeu)"
                m21_status_text.color = ft.Colors.ORANGE_400

            # Reinicia ELM327 e espera o loop OBD2 retomar
            try:
                state.protocol.initialize()
            except Exception:
                pass

            m21_snap_btn.disabled = False
            _safe_page_update()

        threading.Thread(target=_do_snap, daemon=True).start()

    analysis_start_btn.on_click = _on_analysis_start
    analysis_stop_btn.on_click  = _on_analysis_stop
    m21_snap_btn.on_click       = _on_m21_snapshot

    # ── Real-time chart infrastructure (canvas-based for Flet 0.85) ──────────
    import flet.canvas as _cv_mod

    # Simple data series: just a list of (x, y) tuples + color + stroke_width
    class _Series:
        def __init__(self, color, width=2):
            self.color = color
            self.width = width
            self.points: list = []  # list of (x, y)

    _cs_coolant = _Series(ft.Colors.ORANGE_400, 3)
    _cs_o2_b1s1 = _Series(ft.Colors.CYAN_400)
    _cs_o2_b1s2 = _Series(ft.Colors.AMBER_300)
    _cs_ltft    = _Series(ft.Colors.GREEN_400)
    _cs_stft    = _Series(ft.Colors.LIGHT_GREEN_300)
    _cs_timing  = _Series(ft.Colors.YELLOW_400, 3)

    # Canvas-based line chart widget
    class _CanvasChart:
        """Wraps a ft.canvas.Canvas and redraws series as polylines."""
        # Shared width updated on page resize (2 charts side-by-side, padding 12, gap 6)
        _shared_w: float = 400.0
        H = 130

        def __init__(self, series_list, min_y, max_y):
            self.series = series_list
            self.min_y = min_y
            self.max_y = max_y
            self._canvas = _cv_mod.Canvas(
                shapes=[],
                height=self.H,
                expand=True,   # no fixed width — fills container
            )
            self.widget = ft.Container(
                content=self._canvas,
                bgcolor=ft.Colors.GREY_900,
                border=_b(1, ft.Colors.GREY_800),
                height=self.H,
                expand=True,
            )

        @classmethod
        def set_page_width(cls, page_width: float) -> None:
            cls._shared_w = max(100.0, (page_width - 30) / 2.0)

        def redraw(self, x_min, x_max):
            shapes = []
            x_range = max(x_max - x_min, 1)
            y_range = max(self.max_y - self.min_y, 0.001)
            W, H = self._shared_w, self.H

            for s in self.series:
                pts = s.points
                if len(pts) < 2:
                    continue
                paint = ft.Paint(color=s.color, stroke_width=s.width, style=ft.PaintingStyle.STROKE)
                # Build path elements
                elems = []
                first = True
                for (px, py) in pts:
                    cx = (px - x_min) / x_range * W
                    cy = H - (py - self.min_y) / y_range * H
                    cy = max(0.0, min(float(H), cy))
                    if first:
                        elems.append(_cv_mod.Path.MoveTo(cx, cy))
                        first = False
                    else:
                        elems.append(_cv_mod.Path.LineTo(cx, cy))
                if elems:
                    shapes.append(_cv_mod.Path(elements=elems, paint=paint))

            self._canvas.shapes = shapes

    chart_coolant = _CanvasChart([_cs_coolant], 20, 110)
    chart_o2      = _CanvasChart([_cs_o2_b1s1, _cs_o2_b1s2], 0, 1.05)
    chart_trim    = _CanvasChart([_cs_ltft, _cs_stft], -22, 22)
    chart_timing  = _CanvasChart([_cs_timing], -15, 42)
    _CanvasChart.set_page_width(page.width or 800)  # initialize before first draw

    _cv_coolant = ft.Text("Temp: --°C", size=10, color=ft.Colors.ORANGE_400)
    _cv_o2      = ft.Text("O2: --", size=10, color=ft.Colors.CYAN_400)
    _cv_trim    = ft.Text("Trim: --", size=10, color=ft.Colors.GREEN_400)
    _cv_timing  = ft.Text("Avanço: --°", size=10, color=ft.Colors.YELLOW_400)

    _h_thermo = ft.Text("🌡 Termostato: aguardando...", size=11, color=ft.Colors.GREY_500)
    _h_lambda = ft.Text("🔵 Lambda B1S1: aguardando...", size=11, color=ft.Colors.GREY_500)
    _h_cat    = ft.Text("🟤 Catalisador: aguardando...", size=11, color=ft.Colors.GREY_500)
    _h_fuel   = ft.Text("⛽ Combustível: aguardando...", size=11, color=ft.Colors.GREY_500)
    _h_timing = ft.Text("⚡ Ignição: aguardando...", size=11, color=ft.Colors.GREY_500)

    charts_section = ft.Column([
        ft.Text("📊 Gráficos em tempo real:", size=12, weight=ft.FontWeight.BOLD),
        ft.Row([
            ft.Column([
                ft.Row([ft.Text("🌡 Temp. refrigerante", size=10, color=ft.Colors.ORANGE_300), _cv_coolant],
                       alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
                chart_coolant.widget,
            ], expand=True),
            ft.Column([
                ft.Row([ft.Text("🔵 Sonda O2 (B1S1 cyan · B1S2 amber)", size=10, color=ft.Colors.CYAN_300), _cv_o2],
                       alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
                chart_o2.widget,
            ], expand=True),
        ], spacing=6),
        ft.Row([
            ft.Column([
                ft.Row([ft.Text("⛽ Fuel Trim (LTFT verde · STFT claro)", size=10, color=ft.Colors.GREEN_300), _cv_trim],
                       alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
                chart_trim.widget,
            ], expand=True),
            ft.Column([
                ft.Row([ft.Text("⚡ Avanço de ignição", size=10, color=ft.Colors.YELLOW_300), _cv_timing],
                       alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
                chart_timing.widget,
            ], expand=True),
        ], spacing=6),
        ft.Container(
            content=ft.Row([
                ft.Column([_h_thermo, _h_lambda], spacing=6, expand=True),
                ft.Column([_h_cat, _h_fuel, _h_timing], spacing=6, expand=True),
            ]),
            padding=8,
            border=_b(1, ft.Colors.GREY_800),
            border_radius=6,
            margin=ft.margin.Margin(0, 4, 0, 0),
        ),
    ], spacing=4)

    view_analysis = ft.Container(
        padding=12,
        content=ft.Column([
            ft.Text("Análise de Consumo — Logan 1.0L", size=20,
                    weight=ft.FontWeight.BOLD),
            ft.Row([fuel_dd, engine_dd], spacing=8),
            ft.Row([analysis_start_btn, analysis_stop_btn, m21_snap_btn],
                   spacing=8, wrap=True),
            ft.Row([m21_status_text, csv_path_text], spacing=12, wrap=True),
            ft.Divider(height=6),
            # Consumo ao vivo
            ft.Container(
                content=ft.Column([
                    consumption_text,
                    kmL_text,
                    ft.Row([dist_text, fuel_used_text, session_time_text],
                           wrap=True, spacing=12),
                ], spacing=2, horizontal_alignment=ft.CrossAxisAlignment.CENTER),
                padding=10,
                border=_b(1, ft.Colors.CYAN_800),
                border_radius=8,
            ),
            ft.Divider(height=4),
            ft.Text("Parâmetros ao vivo:", size=12, weight=ft.FontWeight.BOLD),
            live_params_col,
            ft.Divider(height=4),
            charts_section,
            ft.Divider(height=4),
            ft.Text("Alertas:", size=12, weight=ft.FontWeight.BOLD),
            ft.Container(alerts_col, height=120, clip_behavior=ft.ClipBehavior.ANTI_ALIAS),
            ft.Divider(height=4),
            ft.Text("Relatório Final:", size=12, weight=ft.FontWeight.BOLD),
            report_col,
        ], scroll=ft.ScrollMode.AUTO, expand=True),
    )

    # =====================================================================
    # LAYOUT PRINCIPAL
    # =====================================================================
    views = [view_connect, view_scan, view_renault, view_logger, view_analysis]
    body = ft.Container(content=views[0], expand=True)

    def _on_nav(e):
        body.content = views[e.control.selected_index]
        _safe_page_update()

    nav = ft.NavigationBar(
        selected_index=0,
        on_change=_on_nav,
        destinations=[
            ft.NavigationBarDestination(icon=ft.Icons.USB,              label="Conexão"),
            ft.NavigationBarDestination(icon=ft.Icons.MEDICAL_SERVICES, label="OBD2"),
            ft.NavigationBarDestination(icon=ft.Icons.CAR_REPAIR,       label="Renault"),
            ft.NavigationBarDestination(icon=ft.Icons.SHOW_CHART,       label="Logger"),
            ft.NavigationBarDestination(icon=ft.Icons.LOCAL_GAS_STATION, label="Análise"),
        ],
    )

    _populate_bt_devices()
    _populate_com_ports()
    page.add(body)
    page.navigation_bar = nav
    _safe_page_update()
