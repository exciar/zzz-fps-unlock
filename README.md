# zzz-fps-patcher

Zenless Zone Zero for Android caps the frame rate at 60 FPS. This rootless patcher changes Unity's `Application.targetFrameRate` setter in `libunity.so`. Any request of 60 FPS or more becomes 120 (or the value of `--fps`). Lower requests, such as the 30 FPS loading screens, are left alone. It then re-signs the full split-APK set with your own key.

The setter is found by its instruction pattern, not by a fixed address, so new game versions usually work without changes. If the pattern isn't found exactly once, the patcher stops without writing anything.

**Tested on:** POCO F8 Pro (SM8750), HyperOS 3, Android 16, ZZZ 3.2.0.

| Scene | Result |
| --- | --- |
| Title screen | 119.7 FPS |
| Gameplay | about 88 FPS average. About two-thirds of frames are at 8 ms; the rest are performance-limited at 16 ms. |

Results were measured from SurfaceFlinger frame statistics.

> Modifying the game client may break HoYoverse's terms of service. Use at your own risk. This repo contains no game files.

## Why the package name stays `com.HoYoverse.Nap`

On HyperOS, PowerKeeper writes `miui_refresh_rate=60` for any game that isn't on Xiaomi's list. That holds the display at 60 Hz no matter what the game asks for. The official package name is on that list, so it keeps 120 Hz along with the Game Turbo features (saturation boost, WildBoost). A renamed clone gets none of these.

## Requirements

- Python 3.10 or newer
- Java 17 or newer (the one bundled with Android Studio, `jbr`, works)
- Android SDK build-tools 35 or newer (`zipalign -P` and `apksigner`)
- `adb`

## First install

1. **Pull the official APKs** from a phone that has the official game installed:
   ```bash
   adb shell pm path com.HoYoverse.Nap
   ```
   Then run `adb pull <path>` into an empty folder for every line it prints. You need `base.apk` plus all the `split_*.apk` files.

2. **Build:**
   ```bash
   python zzz_fps_patcher.py build --apks official --out out --build-tools <sdk>/build-tools/36.0.0 --keystore my-zzz.p12 --ks-pass file:my-zzz.password
   ```
   - The key is created if it doesn't exist. **Back it up.** Future updates must be signed with the same key.
   - Input APKs must carry the official signing certificate. This blocks tampered downloads.

3. **Save the game data.** Uninstalling the official app deletes about 35 GB of downloaded assets, so copy them out first:
   ```bash
   adb shell cp -R /sdcard/Android/data/com.HoYoverse.Nap/files /sdcard/ZZZ-files-backup
   ```

4. **Swap the apps.**
   1. Uninstall the official app.
   2. Run the `adb install-multiple -r ...` command printed by the build.
   3. Open the game once so Android creates its data folder, then close it before it starts downloading.
   4. Copy the data back:
      ```bash
      adb shell cp -R /sdcard/ZZZ-files-backup/. /sdcard/Android/data/com.HoYoverse.Nap/files/
      ```

   The tested migration copied the data from another app's folder rather than from `/sdcard`. The flow above follows the same steps.

5. **In-game:** go to Settings → Graphics and set the frame rate to **60**. The patch runs it at 120.

## Game updates

Google Play can't update the patched app, because it's signed with a different key.

1. Get the new version's official split APKs, either pulled from another device or from a mirror that keeps Google's signature. The signer check rejects anything else.
2. Run `build` again with the **same** keystore.
3. Run `adb install-multiple -r ...`. The app updates in place and keeps your data.

If the build reports that the setter wasn't found, Unity's code changed and the pattern in `zzz_fps_patcher.py` needs updating.

## Check the frame rate

```bash
adb shell dumpsys SurfaceFlinger --timestats -clear -enable
```

Play for 20 seconds, then run:

```bash
adb shell dumpsys SurfaceFlinger --timestats -dump
```

Find the `SurfaceView[com.HoYoverse.Nap/...]` layer. 120 FPS shows as `totalFrames` of about 120 × seconds, with most `present2present` intervals at `8ms`. When you're done, turn collection off:

```bash
adb shell dumpsys SurfaceFlinger --timestats -disable
```

## Self-check

```bash
python test_zzz_fps_patcher.py
```

This patches a synthetic ELF containing the real setter bytes. It runs the injected stub for edge-case requests and confirms the patcher refuses inputs that have no setter, two setters, or are already patched.

`python zzz_fps_patcher.py lib libunity.so out.so` patches a bare library. On 3.2.0 it reproduces the reference build byte for byte (SHA-256 `5e303ae9…2560`).
