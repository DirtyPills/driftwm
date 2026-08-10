# 🖥️ CONTEXT GUI — Linux Mint (driftwm + Xfce)

> **ВАЖНОЕ ПРАВИЛО ДЛЯ ЛЮБОГО ИИ / АГЕНТА:**
> 1. На этом компьютере установлены **две рабочие графические среды**: **driftwm (Wayland)** и **Xfce (X11)**.
> 2. **НЕЛЬЗЯ** ломать, удалять или изменять конфигурации **Xfce**!
> 3. Экран входа в систему (**LightDM / slick-greeter**) ОБЯЗАН сохранять свободный выбор сессии (**driftwm** или **Xfce Session**) при каждом входе в систему.

---

## 📌 1. Архитектура и Дисплей Менеджер (LightDM)

- **Служба входа**: `lightdm.service` (`display-manager.service` → `/lib/systemd/system/lightdm.service`).
- **Сессии в меню входа** (строго по 1 файлу каждого типа, без дубликатов):
  - Wayland-сессия: `/usr/share/wayland-sessions/driftwm.desktop`
  - X11-сессия: `/usr/share/xsessions/xfce.desktop`
- **Запуск driftwm**: происходит через wrapper `/usr/local/bin/driftwm-session`, который запускает `driftwm.service` в пользовательском cgroup через `systemctl --user`.

---

## 🛠️ 2. Конфигурация и Структура driftwm

- **Композитор**: `/usr/local/bin/driftwm` (собран из Rust 1.97).
- **X11 сателлит для Wayland**: `/usr/local/bin/xwayland-satellite` (собран и установлен, обеспечивает работу X11/Qt приложений).
- **Главный конфиг**: `~/.config/driftwm/config.toml`

### Ключевые параметры дрифта (`config.toml`):
- `[session]`:
  - `suspend_on_close = false`
  - `restore_windows = false`
  - `restore_camera = false`
  *(Гарантирует чистый холст при запуске без застрявших старых окон/заглушек)*.
- `[input.keyboard]`:
  - `layout = "us,ru"`
  - `options = "grp:alt_shift_toggle,grp_led:caps"`
  *(Быстрое нативное переключение языка по Alt+Shift без задержек)*.
- `[keybindings]`:
  - `mod+return` → `alacritty`
  - `mod+d` → `nwg-menu` (нативный Wayland Пуск с категориями, поиском и кнопками управления)
  - `mod+space` → `switch-layout next`
  - `mod+l` → `~/.config/driftwm/lock.sh`
  - `mod+n` → `swaync-client -t`

---

## 🌿 3. Нижняя Панель и Меню Пуск (Mint Style)

- **Панель**: `waybar` (`~/.config/waybar/bottom.jsonc` + `~/.config/waybar/bottom.css`).
- **Кнопка "🌿 Пуск"**: вызывает `nwg-menu`.
- **Меню программ (`nwg-menu`)**:
  - Конфиг и стиль: `~/.config/nwg-menu/style.css` (Mint Everforest оформление).
  - Нативный GTK3+layer-shell лаунчер, не создающий лишних окон и подменю на холсте. Обеспечивает классические категории, мгновенный текстовый поиск программ, автозакрытие при клике снаружи (`-k`) и кнопки управления питанием.
- **Индикатор языка в баре**: `~/.config/waybar/lang.sh` (выводит `RU` или `EN` по данным IPC `driftwm msg layout`).

---

## 🚀 4. Запуск сторонних Qt/X11 программ (AmneziaVPN и др.)

- Для Qt приложений в `config.toml` прописано:
  ```toml
  [env]
  MOZ_ENABLE_WAYLAND = "1"
  QT_QPA_PLATFORM = "wayland;xcb"
  ```
- Для **AmneziaVPN** (из-за отсутствия встроенного Wayland Qt плагина в бинарнике CQtDeployer) в `/opt/AmneziaVPN/client/AmneziaVPN.sh` и `/usr/share/applications/AmneziaVPN.desktop` задан `QT_QPA_PLATFORM=xcb`.
- Для трансляции X11/xcb окон на холст Wayland собран и прописан `/usr/local/bin/xwayland-satellite`.
