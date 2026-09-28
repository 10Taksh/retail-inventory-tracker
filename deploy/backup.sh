#!/usr/bin/env bash
# Nightly consistent copy of the SQLite database plus the invoice PDFs; keeps the last 14.
# Installed to /usr/local/bin/retail-inventory-backup by deploy/setup.sh and run from cron.

set -euo pipefail

DB="${DATABASE_PATH:-/var/lib/retail-inventory/inventory.db}"
INVOICES="${INVOICES_DIR:-/var/lib/retail-inventory/invoices}"
BACKUP_DIR="${BACKUP_DIR:-/var/lib/retail-inventory/backups}"
KEEP="${KEEP:-14}"

mkdir -p "$BACKUP_DIR"
stamp=$(date +%Y%m%d-%H%M%S)

if [[ -f "$DB" ]]; then
  # .backup takes a consistent snapshot even while the app is writing.
  sqlite3 "$DB" ".backup '$BACKUP_DIR/inventory-$stamp.db'"
  gzip -f "$BACKUP_DIR/inventory-$stamp.db"
fi

if [[ -d "$INVOICES" ]]; then
  tar -czf "$BACKUP_DIR/invoices-$stamp.tar.gz" -C "$(dirname "$INVOICES")" "$(basename "$INVOICES")"
fi

for pattern in 'inventory-*.db.gz' 'invoices-*.tar.gz'; do
  ls -1t "$BACKUP_DIR"/$pattern 2>/dev/null | tail -n +"$((KEEP + 1))" | xargs -r rm -f
done
