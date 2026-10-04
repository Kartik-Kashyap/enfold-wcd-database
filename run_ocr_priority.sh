#!/bin/bash
# Priority-order OCR launcher for the Oracle box.
#
# Runs states delhi -> up -> odisha -> cg -> bihar, full fidelity 150 DPI and
# correct per-state langs (odisha includes 'ori').  Launch with:
#   setsid nohup ./run_ocr_priority.sh > logs/run_ocr.log 2>&1 < /dev/null &
# so it survives the ssh session ending.  Pass state keys to run a subset:
#   ./run_ocr_priority.sh odisha bihar
#
# Why this retries.  The first Delhi pass was OOM-killed at document 64 of 563
# after 21 hours of work.  The old loop moved straight on to the next state and
# nothing ever came back for the 500 documents still outstanding -- worse, it
# ended by printing "ALL STATES DONE" over an unfinished state, so the run
# looked clean.  `run.py ocr` resumes from processed_docs.json, and that file
# is written temp-file + os.replace, so a SIGKILL cannot truncate it: re-running
# the same state picks up at the first unprocessed PDF.  Retrying is therefore
# both safe and all that is needed.
#
# The progress guard is what stops retrying from becoming a spin.  A document
# that kills the process every time would otherwise be retried forever without
# ever advancing, so an attempt that banks no new documents ends the retries
# for that state and reports the filename it died on.
#
# Env knobs: MAX_ATTEMPTS (default 3), RETRY_DELAY seconds (default 30).
set -u

SCRAPER_DIR=${SCRAPER_DIR:-/home/kartik/scraper}
PY=${PY:-/home/kartik/scraper-venv/bin/python}
MAX_ATTEMPTS=${MAX_ATTEMPTS:-3}
RETRY_DELAY=${RETRY_DELAY:-30}
STATES="${*:-delhi up odisha cg bihar}"

cd "$SCRAPER_DIR" || exit 1
mkdir -p logs

# Documents banked for a state so far.  This is the resume file itself, so the
# count is exactly what a retry would skip past.  Non-numeric output (a python
# that failed to start, a half-written file) reads as 0 -- the guard must never
# spin on a number it could not read.
processed_count() {
  local f="$1/processed_docs.json" n
  [ -f "$f" ] || { echo 0; return; }
  n=$("$PY" -c 'import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        print(len(json.load(fh)))
except Exception:
    print(0)' "$f" 2>/dev/null)
  case "$n" in
    ''|*[!0-9]*) echo 0 ;;
    *) echo "$n" ;;
  esac
}

declare -A RESULT
overall=0

for s in $STATES; do
  state_log="logs/ocr_${s}.log"
  attempts=0
  banked=$(processed_count "$s")
  start_banked=$banked
  status=""

  while :; do
    attempts=$((attempts + 1))
    before_attempt=$banked
    echo "===== STATE $s attempt $attempts/$MAX_ATTEMPTS $(date) ====="
    free -m | sed -n '2p'
    echo "  banked so far: $banked"

    # tee so the state's output is both in this log and per-state (the retry
    # needs the last [n/m] line to name the document that killed the run).
    "$PY" -u run.py ocr --state "$s" 2>&1 | tee -a "$state_log"
    rc=${PIPESTATUS[0]}

    after=$(processed_count "$s")
    echo "===== STATE $s attempt $attempts exit $rc  banked $banked -> $after $(date) ====="

    if [ "$rc" -eq 0 ]; then
      status="ok"
      banked=$after
      break
    fi

    if [ "$after" -gt "$banked" ]; then
      # Real forward progress, so this was a transient kill rather than a
      # document we cannot get past.  Only worth another attempt if we still
      # have one left.
      if [ "$attempts" -ge "$MAX_ATTEMPTS" ]; then
        status="gave-up"
        banked=$after
        break
      fi
      banked=$after
      echo "  exit $rc but banked $after (+$((after - before_attempt))); retrying $s in ${RETRY_DELAY}s"
      sleep "$RETRY_DELAY"
      continue
    fi

    # No progress.  A first-attempt zero-progress failure still gets one retry:
    # it can be transient memory pressure from something else on the box rather
    # than this document.  Beyond that, the same document is killing the process
    # every time, and retrying would spin forever without advancing.
    if [ "$attempts" -ge 2 ]; then
      status="stuck"
      banked=$after
      break
    fi

    banked=$after
    echo "  exit $rc with no progress; retrying $s once in ${RETRY_DELAY}s"
    sleep "$RETRY_DELAY"
  done

  gained=$((banked - start_banked))
  RESULT[$s]="$status  banked=$banked (+$gained)  attempts=$attempts"

  if [ "$status" != "ok" ]; then
    overall=1
    stuck_at=$(grep -o '^\[[0-9]*/[0-9]*\] .*' "$state_log" 2>/dev/null | tail -1)
    if [ -n "$stuck_at" ]; then
      echo "  !! $s ended '$status' on: $stuck_at"
      echo "  !! full output: $state_log"
    else
      echo "  !! $s ended '$status' (see $state_log)"
    fi
  fi
done

echo
echo "===== SUMMARY $(date) ====="
for s in $STATES; do
  echo "  $s: ${RESULT[$s]}"
done

if [ "$overall" -eq 0 ]; then
  echo "===== ALL STATES COMPLETE $(date) ====="
else
  echo "===== FINISHED WITH INCOMPLETE STATES $(date) ====="
  exit 1
fi
