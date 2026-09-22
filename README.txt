CATTLE & SMALL STOCK SYSTEM
Health Data Matrics (HDM Group)
===============================

Four herds in one program — cattle, goats, sheep and a piggery — each with
its own records: herd register, inventory, breeding, billing, expenses,
calendar, analytics, backups, and the HerdIQ assistant.

It runs on this computer. Nothing is sent anywhere: the program listens on
127.0.0.1, which is this machine and nothing else, and your records stay in
a folder under your own user account.


There are two ways to put it on a computer. Most people want the first.

  A.  The Windows installer — CattleSmallStock-Setup-1.0.0.exe. Double-click
      it, type the passkey, and it installs like any other program, with its
      own icon on the desktop and in the Start menu. No Python, nothing else
      to install. Building it is section 8.

  B.  Run it from Python, on any of Windows, macOS or Linux. That is what
      sections 1 to 3 describe.


-------------------------------------------------------------------------
1. INSTALL  (once, on each computer)
-------------------------------------------------------------------------

You need Python 3.9 or newer.

  Windows   https://www.python.org/downloads/
            During setup, tick "Add python.exe to PATH".
  macOS     https://www.python.org/downloads/  (or: brew install python)
  Linux     sudo apt install python3 python3-pip

Then, in this folder, install what the app needs:

  Windows   py -3 -m pip install -r requirements.txt
  macOS     python3 -m pip install -r requirements.txt
  Linux     python3 -m pip install -r requirements.txt

Or let the launcher do it for you the first time:

  python run_desktop.py --install


-------------------------------------------------------------------------
2. ACCESS KEY  (once, on each computer)
-------------------------------------------------------------------------

The first time the system runs on a computer it asks for your access key.
Type it once and that computer is set up; it goes straight in from then on.

  Key:  smallstock@!hdm@livestock@123!@

Keep it with your licence paperwork, not taped to the screen. Ask Health
Data Matrics (HDM Group) if it is lost.

The unlock is remembered in "activation.json" in your data folder (section
4). What is written there is tied to that computer and that user account, so
copying the file to another machine does not open it there — each computer
is unlocked with the key, once. Deleting the file simply means the key is
asked for again; your herd records are untouched.

Renaming the computer, or moving the data folder somewhere else, also means
the key is asked for once more. That is normal.

To set up several computers without anyone typing it, start with:

  python run_desktop.py --key "smallstock@!hdm@livestock@123!@"

The key itself is never written into the program or onto the disk. The
program holds only a fingerprint of it, which is enough to recognise the
right key and not enough to work out what it is.

Be clear about what this is: it stops the system being opened by someone who
has simply been handed a copy. It is not encryption. The program is readable
Python, so anyone willing to edit it can cut the lock out, and the herd
records are ordinary database files. Protect the computer and the backups
themselves — a screen lock, and backups kept somewhere private.


-------------------------------------------------------------------------
3. START IT
-------------------------------------------------------------------------

  Windows   double-click  Start Cattle and Small Stock.bat
  macOS     double-click  start-cattle-and-small-stock.command
            (first time only: right-click it, choose Open, then Open again)
  Linux     ./start-cattle-and-small-stock.sh

  If macOS or Linux says the launcher is not executable, the unzip tool
  dropped the permission. Fix it once, in this folder:
      chmod +x start-cattle-and-small-stock.command start-cattle-and-small-stock.sh

A console window opens and reports what it is doing, then the app appears.
KEEP THE CONSOLE WINDOW OPEN while you work — closing it stops the app.
Press Ctrl-C in it, or close the app window, to stop.

For a real application window instead of a browser tab, install pywebview
once:

  python -m pip install pywebview

The launcher then uses it automatically. Without it, the app opens in your
default browser, which works just as well.

Options:

  python run_desktop.py --browser        force the browser
  python run_desktop.py --port 8600      use a different port
  python run_desktop.py --data D:\Herds  keep the records in a chosen folder
  python run_desktop.py --install        install the packages first
  python run_desktop.py --key KEY        unlock without anyone typing it
  python run_desktop.py --selfcheck      check everything it needs is here,
                                         say so, and exit without starting

If port 8501 is busy, the launcher moves to the next free one by itself.


-------------------------------------------------------------------------
4. WHERE YOUR RECORDS LIVE
-------------------------------------------------------------------------

Each herd has its own database file, side by side:

  cattle.db   goats.db   sheep.db   pigs.db

By default they sit in a per-user folder, which survives reinstalling the
app, and holds the unlock record too:

  Windows   %LOCALAPPDATA%\HDM Cattle Management
  macOS     ~/Library/Application Support/HDM Cattle Management
  Linux     ~/.local/share/HDM Cattle Management

To keep them somewhere else — a USB stick, a shared drive, a synced folder —
start with:  python run_desktop.py --data "E:\Herd records"

Use the same --data folder every time, or the app will look in the default
place and appear empty.


-------------------------------------------------------------------------
5. SAVING FILES, PHOTOS AND BACKUPS
-------------------------------------------------------------------------

DOWNLOADS
  Every "Download" button writes the file straight to your Downloads folder
  and then tells you the full path. Nothing is left sitting in a browser.
  A file with a name that already exists is saved as "... (2)" rather than
  overwriting. To send exports elsewhere, set CATTLE_DOWNLOADS to a folder
  before starting.

  PDFs: herd inventory, animal profile, day report, sale invoice, purchase
  receipt and HerdIQ answers. Spreadsheets: CSV and Excel throughout.

PHOTOS
  One passport-style photo per animal, added on the Load and Edit screens.
  Photos are scaled down to 700px and stored inside the herd database, so
  they travel with your backups. The farm logo works the same way and is
  printed on every report.

BACKUPS
  Backup tab -> Download a backup   a dated .zip holding the database and a
                                    CSV of every table
             -> Save to a drive     writes the same thing to a folder you
                                    name: a USB stick, a network share, a
                                    synced folder. Each backup goes into its
                                    own dated sub-folder, so nothing is
                                    overwritten.
             -> Restore             takes the backup .zip, or the .db file
                                    from inside it. Your current data is
                                    kept as a ".replaced-<date>" file first,
                                    so a restore can be undone.

  Back up at the end of any day you recorded sales, births or new animals,
  and keep at least one copy off this computer.


-------------------------------------------------------------------------
6. IF SOMETHING GOES WRONG
-------------------------------------------------------------------------

The app will not start
  Run it directly to see the error:
      python -m streamlit run cattlemanagementapp.py

"Python is not installed, or not on the PATH"
  Reinstall Python and tick "Add python.exe to PATH" (Windows), or use the
  full path to python.exe in the .bat file.

The window is blank
  Give it a few more seconds on the first run, then reload. If it stays
  blank, open http://127.0.0.1:8501 yourself.

The herd looks empty
  You are probably pointing at a different data folder — see section 4.

Nothing downloads
  Check the message under the button: it names the exact path the file went
  to, or says what stopped it.


-------------------------------------------------------------------------
7. WHAT IS IN THIS FOLDER
-------------------------------------------------------------------------

The program
  cattlemanagementapp.py            the application
  cattlemanagement_generator1.py    the source generator: running it writes
                                    cattlemanagementapp.py, requirements.txt
                                    and .streamlit/config.toml again. Edit
                                    this, not the app file.
  run_desktop.py                    the desktop launcher — and, in the built
                                    .exe, the program's entry point
  requirements.txt                  the packages it needs
  requirements-windows.txt          the two extra ones that draw the window
  .streamlit/config.toml            colours and window settings
  logo.ico                          the system icon: the four herds together
                                    at the large sizes, the cattle head alone
                                    at the small ones, where only one shape
                                    survives

Starting it from Python
  Start Cattle and Small Stock.bat  Windows launcher
  start-cattle-and-small-stock.command   macOS launcher
  start-cattle-and-small-stock.sh   Linux launcher

Building the Windows installer  (section 8)
  build_windows.spec                the PyInstaller recipe
  installer.iss                     the Inno Setup script, with the passkey
                                    gate
  make_key.py                       turns a passkey into the fingerprint the
                                    installer carries
  .github/workflows/build-windows.yml
                                    builds all of it on GitHub, on demand

  README.txt                        this file

Your data folder also holds activation.json — the record that this computer
has been unlocked. It is not part of this folder and is not copied when you
move the program.


-------------------------------------------------------------------------
8. BUILDING THE WINDOWS INSTALLER
-------------------------------------------------------------------------

This turns the whole system into one CattleSmallStock-Setup-1.0.0.exe that
installs on a computer with no Python on it at all.

ON GITHUB  (nothing to install)

  Push this folder to a GitHub repository, open the Actions tab, choose
  "Build Windows installer" and press Run workflow. About fifteen minutes
  later the finished installer is waiting under Artifacts. Pushing a tag
  that starts with v — v1.0.0, say — builds it too.

ON A WINDOWS MACHINE

  py -3 -m pip install -r requirements.txt -r requirements-windows.txt
  py -3 -m pip install --upgrade "pyinstaller>=6.14"
  py -3 cattlemanagement_generator1.py
  pyinstaller --noconfirm --clean build_windows.spec
  dist\CattleSmallStock\CattleSmallStock.exe --selfcheck --report check.txt
  "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer.iss

  The program lands in dist\CattleSmallStock\ and the installer in
  installer_output\. Any Inno Setup 6 will do — the script asks the compiler
  its version and adapts.

  Do not skip the --selfcheck line. The built program has no console window,
  so a library that did not make it into the bundle is invisible until
  somebody installs it and it dies on start. That line imports everything the
  app imports, does arithmetic with numpy, writes a PDF and compiles the app,
  then writes check.txt and returns a failure code if anything is wrong. The
  GitHub build runs it for you, along with starting the built program and
  fetching a page from it.

WHAT THE INSTALLER ASKS FOR

  The passkey, before it will copy a single file. It is the same key the
  system itself asks for:

      smallstock@!hdm@livestock@123!@

  The installer does not contain that key — only a SHA-256 fingerprint of
  it, which recognises the right key and cannot be turned back into it. When
  the passkey is accepted, the installer hands it straight to the program it
  just installed, so nobody types it twice; the program turns it into a
  record tied to that computer and forgets it.

CHANGING THE PASSKEY

  py -3 make_key.py

  It asks for the new key without showing it, and prints two lines: one to
  paste into installer.iss at MyPasskeyHash, one into
  cattlemanagement_generator1.py at ACCESS_KEY_FINGERPRINT. Then regenerate
  the app and rebuild. Both must carry the same fingerprint, or the
  installer will accept a key the program then refuses.

  The key itself is written in this README and nowhere else in the project.
