#!/usr/bin/env python3
"""Turn the installation passkey into the fingerprint the installer carries.

The installer never holds the key itself — only this SHA-256 of it, which is
enough to recognise the right key at the passkey screen and not enough to
work out what the key is.

    python make_key.py                  asks for the key without showing it
    python make_key.py "the key"        for a script; it lands in your shell
                                        history, so prefer the first form

Paste the line it prints into installer.iss, at MyPasskeyHash.

The key itself is deliberately not written in this file, or in any other file
in this project. Nothing here can tell you what the key is; it can only
confirm one you already know.
"""
import getpass
import hashlib
import sys


def fingerprint(key):
    return hashlib.sha256(key.strip().encode("utf-8")).hexdigest()


def main():
    if len(sys.argv) > 1:
        key = sys.argv[1]
    else:
        key = getpass.getpass("  Passkey (not shown): ")
        again = getpass.getpass("  Type it again:       ")
        if key.strip() != again.strip():
            raise SystemExit("\n  Those do not match. Nothing was changed.\n")
    if not key.strip():
        raise SystemExit("\n  No key given.\n")
    digest = fingerprint(key)
    print()
    print("  SHA-256     ", digest)
    print()
    print("  In installer.iss:")
    print(f'      #define MyPasskeyHash "{digest}"')
    print()
    print("  In the app source (cattlemanagement_generator1.py):")
    print(f'      ACCESS_KEY_FINGERPRINT = "{digest}"')
    print()
    print("  Keep the key itself with your licence paperwork. Anyone holding")
    print("  this file can check a guess, but cannot read the key out of it.")
    print()


if __name__ == "__main__":
    main()
