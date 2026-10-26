#!/usr/bin/env python3
"""
switch-domain.py - Seamless Domain Switcher & Synchronizer
==========================================================
Enables instant migration across entire personal website repo when changing domains.

Usage:
    python scripts/switch-domain.py --from 11231632.xyz --to lmlu.de
    python scripts/switch-domain.py --from lmlu.de --to newdomain.com --dry-run
"""

import os
import sys
import argparse
from pathlib import Path

# Subdomains commonly deployed across the fleet
SUBDOMAINS = [
    "ip",
    "bin",
    "status-k9x2",
    "mail",
    "token",
    "sci",
    "bot",
    "sub",
    "sjc",
    "aws-us",
    "az",
    "aws",
    "sg",
    "fra",
    "mrs",
    "mad",
    "cdn-sjc",
    "cdn-fra",
    "cdn-mad",
    "www"
]

TARGET_EXTENSIONS = {".html", ".conf", ".yml", ".yaml", ".json", ".md", ".env", ".example", ".sh", ".py"}

def process_file(file_path: Path, old_domain: str, new_domain: str, dry_run: bool = False):
    try:
        content = file_path.read_text(encoding="utf-8")
    except Exception:
        return 0

    new_content = content
    # 1. Replace subdomains first to prevent double-prefix replacement
    for sub in SUBDOMAINS:
        old_sub = f"{sub}.{old_domain}"
        new_sub = f"{sub}.{new_domain}"
        new_content = new_content.replace(old_sub, new_sub)

    # 2. Replace email catch-all pattern *@old_domain
    new_content = new_content.replace(f"*@{old_domain}", f"*@{new_domain}")
    new_content = new_content.replace(f"@{old_domain}", f"@{new_domain}")

    # 3. Replace apex domain
    new_content = new_content.replace(old_domain, new_domain)

    if content != new_content:
        diff_count = content.count(old_domain)
        print(f"[{'DRY-RUN' if dry_run else 'UPDATED'}] {file_path.relative_to(file_path.parents[2] if len(file_path.parents) > 2 else file_path.parent)} (~{diff_count} occurrences)")
        if not dry_run:
            file_path.write_text(new_content, encoding="utf-8")
        return 1
    return 0

def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="One-click domain migration tool for personal website.")
    parser.add_argument("--from", dest="old_domain", required=True, help="Current domain (e.g. 11231632.xyz)")
    parser.add_argument("--to", dest="new_domain", required=True, help="New target domain (e.g. lmlu.de)")
    parser.add_argument("--dry-run", action="store_true", help="Preview modifications without writing to disk")
    args = parser.parse_args()

    root_dir = Path(__file__).resolve().parent.parent
    print(f"=== Domain Migration: '{args.old_domain}' -> '{args.new_domain}' ===")
    print(f"Root workspace: {root_dir}")
    if args.dry_run:
        print("Mode: DRY-RUN (no files will be modified)\n")
    else:
        print("Mode: LIVE WRITE\n")

    changed_files = 0
    for root, dirs, files in os.walk(root_dir):
        # Skip git directory
        if ".git" in dirs:
            dirs.remove(".git")
        for f in files:
            p = Path(root) / f
            if p.suffix.lower() in TARGET_EXTENSIONS or f.startswith(".env"):
                changed_files += process_file(p, args.old_domain, args.new_domain, dry_run=args.dry_run)

    print(f"\n[OK] Completed! Total files affected: {changed_files}")

if __name__ == "__main__":
    main()
