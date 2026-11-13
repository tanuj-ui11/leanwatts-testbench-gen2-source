# LeanWatts theme reuse

The user supplied `LeanWatts-EoL-0.1.0-win64 (1)/LeanWatts EoL` and explicitly limited reuse to **its theme only**. The new charger calibration application uses the theme and fonts listed below. Its calibration workflow and screen layouts are separate implementations based on this project's requirements. The supplied executable was not launched and its files remain unchanged.

## Assets reused directly

| Source in supplied package | New application destination | Treatment |
|---|---|---|
| `_internal/app/lw_eol/app/theme.py` | `leanwatts_can/theme.py` | Exact copy of visual tokens, styles and reusable Card/Tile/table helpers |
| `_internal/app/lw_eol/app/fonts/Outfit.ttf` | `leanwatts_can/fonts/Outfit.ttf` | Exact copy |
| `_internal/app/lw_eol/app/fonts/JetBrainsMono.ttf` | `leanwatts_can/fonts/JetBrainsMono.ttf` | Exact copy |
| `_internal/app/lw_eol/app/fonts/OFL-Outfit.txt` | `leanwatts_can/fonts/OFL-Outfit.txt` | Font license retained |
| `_internal/app/lw_eol/app/fonts/OFL-JetBrainsMono.txt` | `leanwatts_can/fonts/OFL-JetBrainsMono.txt` | Font license retained |

`theme.py` imports standard-library modules and PySide6 only at module import. Its `style_plot` helper imports pyqtgraph only when invoked. It resolves fonts relative to its own file, so the theme can be reused independently. It does not import `lw_eol`, construct `EolApp`, initialize a transport or communicate with hardware.

## Reuse boundary and layout requirement

Only the theme module and font assets are copied from the original application. Its operator screen, navigation implementation, recipe workflows, reports, login/roles, instrument drivers, calibration logic and hardware startup code are not reused. The new application does not import any `lw_eol` application modules.

The user requires a compact application with **no full-window or full-page scrolling**. A section may scroll when unavoidable, for example a table or log. This is a requirement for the new layout, not an inherited behavior from the reference application.

The supplied generic Python runtime and third-party PySide6 dependencies may be used to run the independent companion application. Reusing those runtime libraries does not import the original application's workflow or device-control code.

## Exact visual tokens

Window `#110E18`; title `#0C0A12`; cards `#1C1828`; inputs `#252034`; alternate rows `#201B2E`; borders `#2A2440` and `#3A3253`; text `#F3F0FA`; secondary text `#ABA5C0`; muted `#8E88A6`; accent `#A78BFA`; success `#6EE7A8`; danger `#FF8A8A`; warning `#F5B656`. UI font Outfit; technical values/logs JetBrains Mono. Card radius 18 px, input radius 10 px, button radius 12 px, live tile radius 14 px. Card interior margins are 22/18/22/20 px with 12 px spacing.

## Separation from hardware initialization

The new application is independent of the original `services/app.py`, `services/runtime.py`, instrument factories and startup path. Reusing the theme does not establish Maynuo protocol support or CAN-adapter compatibility. The separate protocol evidence and bench validation requirements remain applicable.
