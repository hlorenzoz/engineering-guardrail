#!/bin/sh
# Run a test command and keep its COMPLETE output in tests/tests_<name>.txt.
#
#   sh tests/log.sh <name> <command> [arguments...]
#
# A terminal, a hook or an agent shows only the end of a long run. The file keeps
# all of it, so a failure can be read in full later, with the command, the commit
# and the exit code it belongs to. Each run replaces the file of its name: it is
# the LAST run, not a history. The files are not versioned (.gitignore).
#
# The output still goes to the terminal, and the exit code is the command's own.
set -u

if [ "$#" -lt 2 ]; then
  echo "usage: sh tests/log.sh <name> <command> [arguments...]" >&2
  exit 2
fi

name=$1
shift
root=$(cd "$(dirname "$0")/.." && pwd)
out="$root/tests/tests_$name.txt"
status_file="$out.status"

{
  echo "# command:  $*"
  echo "# started:  $(date '+%Y-%m-%d %H:%M:%S %z')"
  echo "# commit:   $(git -C "$root" rev-parse --short HEAD 2>/dev/null || echo unknown), $(git -C "$root" status --porcelain 2>/dev/null | wc -l | tr -d ' ') uncommitted file(s)"
  echo "# directory: $(pwd)"
  echo
} > "$out"

# Colours off, so the file is plain text. Both streams, in the order they happen.
# The exit code travels through a file: a pipe would report tee's, not the command's.
{
  NO_COLOR=1 FORCE_COLOR=0 "$@" 2>&1
  echo $? > "$status_file"
} | tee -a "$out"

status=$(cat "$status_file" 2>/dev/null || echo 1)
rm -f "$status_file"

# Some tools colour their output anyway: strip what is left, in the file only.
perl -pi -e 's/\e\[[0-9;?]*[A-Za-z]//g' "$out" 2>/dev/null

{
  echo
  echo "# finished: $(date '+%Y-%m-%d %H:%M:%S %z')"
  echo "# exit:     $status"
} >> "$out"

exit "$status"
