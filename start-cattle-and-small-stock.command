#!/bin/bash
# Cattle & Small Stock System - macOS launcher.
# Double-click this file. Keep the Terminal window open while you work.
cd "$(dirname "$0")" || exit 1

PY=""
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1; then PY="$candidate"; break; fi
done

if [ -z "$PY" ]; then
  echo
  echo "Python 3 is not installed."
  echo "Install it from https://www.python.org/downloads/ and run this again."
  echo
  read -r -p "Press Return to close. "
  exit 1
fi

"$PY" run_desktop.py "$@"
status=$?
if [ $status -ne 0 ]; then
  echo
  read -r -p "The app stopped with an error. Press Return to close. "
fi
exit $status
