# OBD2 Logan Scanner & Datalogger

> 🇧🇷 [Português](#português) | 🇺🇸 [English](#english)

---

## English

Professional OBD-II datalogger for Renault Logan 2012 1.0 16v (ISO 9141-2/KWP2000) via Classic Bluetooth SPP dongle (Jieli chip ELM327 clone).

### SOLID Architecture

```
src/
├── transport/       # IObdTransport interface + SPP (Pyjnius) + Mock impl
├── protocol/        # ELM327 (init, send, parse '>' prompt)
├── pids/            # Registry + decoders + YAML definitions
├── scheduler/       # Priority read loop + adaptive pacing
├── storage/         # SQLite WAL + CSV export
├── diagnostics/     # Support scan + VIN + DTCs (mode 03/07) + freeze frame (02)
├── ui/              # Flet screens: Scan, Logger, History
├── config/          # PIDs defined in YAML
└── main.py          # Flet entry point
```

### Applied Principles

- **SRP**: each module has one responsibility. Decoder does not know about transport; transport does not know about protocol.
- **DIP**: scheduler depends on the abstract `IObdTransport`, not on the SPP implementation.
- **OCP**: new PIDs = add YAML entry + decoder, no changes to scheduler.
- **ISP**: small interfaces (`IObdTransport`, `IStorage`, `IPidDecoder`).
- **LSP**: `MockTransport` and `SppTransport` are interchangeable.

### Usage Flow

1. **Home screen**: lists paired devices; user selects one.
2. **Scan mode**: initializes ELM, discovers protocol, reads VIN, builds supported-PID bitmap (0100/0120/0140), reads each supported PID once, reads DTCs (mode 03+07), reads freeze frame (mode 02). Generates report.
3. **Logger mode**: uses PIDs discovered during scan, schedules by priority, writes to SQLite. "Stop" button exports CSV.

### Buildozer (Android)

```
android.permissions = BLUETOOTH, BLUETOOTH_ADMIN, BLUETOOTH_CONNECT, BLUETOOTH_SCAN, ACCESS_FINE_LOCATION, WRITE_EXTERNAL_STORAGE
android.api = 33
requirements = python3, flet, pyjnius, pyyaml
```

---

## Português

Datalogger profissional OBD-II para Renault Logan 2012 1.0 16v (ISO 9141-2/KWP2000) via dongle Bluetooth Classic SPP (chip Jieli clone ELM327).

## Arquitetura SOLID

```
src/
├── transport/       # Interface IObdTransport + impl SPP (Pyjnius) + Mock
├── protocol/        # Elm327 (init, send, parse de '>' prompt)
├── pids/            # Registry + decoders + YAML de definições
├── scheduler/       # Loop de leitura com prioridade + pacing adaptativo
├── storage/         # SQLite WAL + export CSV
├── diagnostics/     # Scan suporte + VIN + DTCs (modo 03/07) + freeze frame (02)
├── ui/              # Flet (telas: Scan, Logger, Histórico)
├── config/          # PIDs definidos em YAML
└── main.py          # Entry point Flet
```

## Princípios aplicados

- **SRP**: cada módulo uma responsabilidade. Decoder não conhece transporte, transporte não conhece protocolo.
- **DIP**: scheduler depende de `IObdTransport` abstrato, não da impl SPP.
- **OCP**: PIDs novos = adicionar entrada YAML + decoder, sem mudar scheduler.
- **ISP**: interfaces pequenas (`IObdTransport`, `IStorage`, `IPidDecoder`).
- **LSP**: `MockTransport` e `SppTransport` intercambiáveis.

## Fluxo de uso

1. **Tela inicial**: lista dispositivos pareados, usuário escolhe.
2. **Modo Scan**: inicializa ELM, descobre protocolo, lê VIN, faz bitmap de PIDs suportados (0100/0120/0140), lê 1x cada PID suportado, lê DTCs (modo 03+07), lê freeze frame (modo 02). Gera relatório.
3. **Modo Logger**: usa PIDs descobertos no scan, agenda por prioridade, grava em SQLite. Botão "parar" exporta CSV.

## Buildozer (Android)

```
android.permissions = BLUETOOTH, BLUETOOTH_ADMIN, BLUETOOTH_CONNECT, BLUETOOTH_SCAN, ACCESS_FINE_LOCATION, WRITE_EXTERNAL_STORAGE
android.api = 33
requirements = python3, flet, pyjnius, pyyaml
```
