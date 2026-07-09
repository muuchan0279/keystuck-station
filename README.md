# KEYSTUCK STATION

**Fixes system-wide stuck keys caused by Proton/Wine's `winebus` probing hidraw devices.**

A small PySide6 GUI that lists every Proton/Wine prefix on your machine, shows whether the
`DisableHidraw` workaround is applied, and lets you apply or revert it per prefix.

![status: works on my machine, and maybe yours](https://img.shields.io/badge/scope-niche%20but%20real-informational)

---

## The symptom

You launch a Steam game through Proton. Suddenly keys get **stuck down** — a held `W`,
a repeating character — and it happens in **every application**, not just the game.
Alt-tabbing out doesn't help. The compositor logs nothing. Restarting the game fixes it
until next time.

## The cause

Wine/Proton's `winebus` driver enumerates **all `hidraw` devices** hunting for gamepads.
If a device can't answer that probe, the kernel's `usbhid` starts **resetting it every
~1.3 seconds**. Key-release events are lost inside those reset windows, so the key latches
down *below* the compositor — which is why every app is affected and why nothing in
KWin/Xwayland/Steam Input logs an error.

On the machine this was found on, the offender was an **ASUS N-KEY built-in keyboard
(`0b05:19b6`)**, whose chip doubles as an RGB controller. Any HID device that chokes on the
probe can trigger it.

Setting `DisableHidraw=1` in the prefix's `winebus` registry key stops the probe, and the
resets stop with it.

## Check whether this is your bug (30 seconds)

Launch the game, then:

```bash
journalctl -k --since "5 minutes ago" | grep -iE "reset .*speed USB"
```

If you see resets repeating **every 1–2 seconds**, this is it. Identify the device:

```bash
cat /sys/bus/usb/devices/<X-Y>/product     # e.g. "N-KEY Device"
```

If you see no resets, your stuck keys have a different cause and this tool won't help.

## Measured effect

One prefix (a Proton game), same machine, same session:

| | USB resets |
|---|---|
| before the fix | **134 in 5 minutes** |
| after the fix, 26 minutes of play | **0** |

The offending device was an ASUS N-KEY built-in keyboard (`0b05:19b6`).
Verified on Fedora/Nobara, KDE Plasma 6 (Wayland).

## What it changes

Exactly one line, in each prefix's `system.reg`:

```
[System\\ControlSet001\\Services\\winebus]
"DisableHidraw"=dword:00000001
```

A backup is written to `system.reg.bak-keystuck` before the first edit.

## Install & run

Requires Python 3 and PySide6:

```bash
pip install --user PySide6
git clone https://github.com/muuchan0279/keystuck-station.git
cd keystuck-station && ./run.sh
```

To add it to your application menu, edit the `Exec=` path in
`keystuck-station.desktop`, copy it to `~/.local/share/applications/`, and run
`update-desktop-database ~/.local/share/applications`.

It scans, in whatever exists: native Steam (`~/.local/share/Steam`, `~/.steam/steam`),
Flatpak Steam, extra Steam library folders from `libraryfolders.vdf`, XIVLauncher
(`~/.xlcore`), Lutris (`~/Games/*/prefix`), and Bottles.

Nothing runs as root. It only writes to prefixes in your home directory.

## Read this before clicking

- **A running game cannot be patched.** While `wineserver` is alive it holds the registry in
  memory and **rewrites `system.reg` on exit**, silently discarding your change. Rows for
  running prefixes are disabled and marked `▶ RUNNING`. Quit the game, then hit `↻`.
- **Side effect:** in a patched prefix, features that talk to `hidraw` directly — DualSense
  adaptive triggers, haptics — stop working. Ordinary keyboard and gamepad input is unaffected.
- **Reinstalling a game wipes the fix**, because the prefix is recreated. New games start
  unpatched. That is the whole reason this tool shows a ledger instead of a single button.
- **`△ NO BUS`** means the prefix has no `winebus` section yet. Launch the game once and it
  will appear.
- `[戻す]` / *Revert* restores from the backup, or removes just that line if the backup is gone.

## Doing it by hand

If you'd rather not run a GUI, this is the whole fix — with the game **fully closed**:

```python
import re
p = "<prefix>/system.reg"
pat = re.compile(r'(\[System\\\\ControlSet001\\\\Services\\\\winebus\][^\n]*\n(?:#time=[^\n]*\n)?)')
text = open(p, encoding="utf-8").read()
m = pat.search(text)
sec_end = text.find("\n[", m.end())
if '"DisableHidraw"' not in text[m.start():sec_end]:
    open(p + ".bak-keystuck", "w", encoding="utf-8").write(text)
    open(p, "w", encoding="utf-8").write(
        text[:m.end()] + '"DisableHidraw"=dword:00000001\n' + text[m.end():])
```

## Things that do *not* work

Time was burned on these, so you don't have to:

- Setting `Start=4` on the `wineusb` service — Wine's PnP ignores it.
- `DllOverrides` `"wineusb.sys"=""` — releases the usbfs grab, but the resets continue,
  because they come from `winebus`'s hidraw probe.
- Disabling the game's own LED/RGB plugins.
- Blaming Steam Input, `input-remapper`, KWin, or Xwayland. All innocent.

## License

MIT. See `LICENSE`.

---

## 日本語

Proton/Wine の `winebus` が全 `hidraw` デバイスを走査し、応答できないUSB機器
(ASUS N-KEY内蔵キーボード `0b05:19b6` など)を刺すと、カーネルが1〜2秒ごとにUSBリセットを
連発する。そのリセット窓で KeyRelease が失われ、**コンポジタより下の層**でキーが押しっぱなしに
なる ― ゲームだけでなく全アプリで詰まるのはこのため。

prefix の `system.reg` に `DisableHidraw=1` を入れれば止まる。ただし prefix はゲームごと・
再インストールごとに作り直されるので、**どこに穴が空いているかを一覧できないと再発に気づけない**。
それがこのアプリ。

自分の症状か確かめる:

```bash
journalctl -k --since "5 minutes ago" | grep -iE "reset .*speed USB"
```

1〜2秒間隔でリセットが並んでいれば当たり。並んでいなければ別の原因なので、これでは直らない。

**注意**: ゲーム起動中の prefix は編集しても `wineserver` 終了時に巻き戻されるため、
ボタンを無効化してある。副作用として、そのprefixでは hidraw 直叩きのパッド機能
(DualSense のアダプティブトリガー等)が無効になる。通常のキーボード・パッド入力は無傷。
