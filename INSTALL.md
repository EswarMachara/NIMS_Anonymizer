# Installing TANUH Renal Anonymizer

For the person who will actually use the app. Three steps, about two minutes.

---

## 1. Download

Go to the [Releases page](https://github.com/EswarMachara/NIMS_Anonymizer/releases)
and click the file named:

```
TANUH-Renal-Anonymizer-Setup-1.0.0.exe
```

That one file is everything. Ignore the `portable` zip below it — that is for
IT, not for you.

## 2. Run it

Double-click the downloaded file.

**Windows will try to stop you the first time.** A blue box appears saying
*"Windows protected your PC"*. This is not a virus warning — it is Windows
saying it has not seen this app before, which is true, because it is an
internal hospital tool rather than something sold to the public.

> Click **More info**, then **Run anyway**.

Then the ordinary installer opens. Click **Next** through it and then
**Install**. If you are logged in as an administrator, Windows may also ask
whether to allow the app to make changes — click **Yes**.

You do **not** need administrator rights. Without them it installs just for
you, which works exactly the same.

## 3. Open it

It is now in the Start Menu. Press the **Windows key**, type `TANUH`, and
press Enter. (There is a desktop shortcut too, unless you unticked it.)

---

## If the app disappears, or will not open

Some antivirus products delete the app the first time you run it, even
though it is safe. This is a known false alarm caused by the app being
**unsigned** — it has no purchased publisher certificate yet — combined with
the way Python applications are packaged. It is not caused by anything the
app does.

You cannot fix this yourself, and you should not try to turn your antivirus
off. Send your IT team
[`desktop_app/build/IT_SECURITY_REVIEW_BRIEF.md`](desktop_app/build/IT_SECURITY_REVIEW_BRIEF.md)
from this repository — it explains what the app is and asks them to
allow-list it centrally, once, for everyone.

## If the app opens but the window is blank

The app draws its screen using **Microsoft Edge WebView2**. Windows 11 always
has it. An older Windows 10 machine might not, in which case the installer
will have already offered you the download link. You can also get it from
Microsoft directly:
<https://developer.microsoft.com/microsoft-edge/webview2/>

---

## Updating later

Download the newer `Setup` file and run it. It replaces the old version in
place — nothing to uninstall first, and **your data is untouched**.

## Uninstalling

Settings → Apps → Installed apps → **TANUH Renal Anonymizer** → Uninstall.

This removes the program only. **Your work is not deleted.** The ID-mapping
CSVs, the record of which files have already been anonymized, and any
anonymized output still sit in:

```
%LOCALAPPDATA%\TANUH-Renal-Anonymizer\
```

Those mapping CSVs still link Anonymized IDs to real patient names and CR
numbers, so keep that folder access-restricted. If this workstation is being
handed on to someone else, delete it deliberately.
