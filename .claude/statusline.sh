#!/bin/bash
# harness project statusline: fleet pipeline + live agents + git + model + tokens.
# Claude Code pipes a JSON status payload on stdin and renders our stdout.
# Overrides the global statusline (~/.claude/statusline-command.sh) within harness.
#
# Design note: this line renders in EVERY harness session, but bd `in_progress`
# and agent worktrees are machine-GLOBAL, not per-session. So we deliberately
# do NOT claim "you are on ticket X" (that was stale between sessions and often
# wrong). We show an honest fleet aggregate instead: how many tickets sit at
# each pipeline stage, and how many agent worktrees are actually live. An
# in_progress count with no live agent to back it is marked stale ("?").

input=$(cat)

cwd=$(echo "$input" | jq -r '.cwd // .workspace.current_dir // empty')
model=$(echo "$input" | jq -r '.model.display_name // empty')
# Used TOKENS (current context: input + cache create + cache read) and the
# window size, so we compute "percent of tokens used" ourselves rather than
# leaning on .used_percentage (which reads as context occupancy and has drifted
# from current usage across versions). Keep used_percentage only as a fallback.
used_tokens=$(echo "$input" | jq -r '.context_window.total_input_tokens // empty')
window=$(echo "$input" | jq -r '.context_window.context_window_size // empty')
used=$(echo "$input" | jq -r '.context_window.used_percentage // empty')

# --- Live agent worktrees (B) -----------------------------------------------
# One worktree per spawned agent under .claude/worktrees/agent-*. Branch encodes
# role (land/* = reviewer|lander, else builder); dir mtime within LIVE_TTL = live
# (stale worktrees linger for days after their agent exits). `git worktree list`
# is cheap but we cache the COMPUTED triple "live total stale" like bd below, so
# every render stays instant. `live` also gates bd staleness (see below).
wt_cache="${TMPDIR:-/tmp}/-wt.cache"
wt_ttl=5
live_agents=0
total_agents=0
land_live=0
if [ -n "$cwd" ] && git -C "$cwd" rev-parse --git-dir > /dev/null 2>&1; then
    now=$(date +%s)
    mtime=0
    [ -f "$wt_cache" ] && mtime=$(stat -c %Y "$wt_cache" 2>/dev/null || echo 0)
    if [ $((now - mtime)) -ge "$wt_ttl" ]; then
        touch "$wt_cache"
        (
            live=0; total=0; landc=0; nowb=$(date +%s); live_ttl=600
            p=""; b=""
            finalize() {
                case "$p" in
                    */.claude/worktrees/agent-*)
                        total=$((total + 1))
                        m=$(stat -c %Y "$p" 2>/dev/null || echo 0)
                        if [ $((nowb - m)) -lt "$live_ttl" ]; then
                            live=$((live + 1))
                            case "$b" in */land/*) landc=$((landc + 1)) ;; esac
                        fi
                        ;;
                esac
                p=""; b=""
            }
            while IFS= read -r line; do
                if [ -z "$line" ]; then finalize; continue; fi
                case "$line" in
                    "worktree "*) p=${line#worktree } ;;
                    "branch "*)   b=${line#branch } ;;
                esac
            done < <(git -C "$cwd" worktree list --porcelain 2>/dev/null)
            finalize
            if echo "$live $total $landc" > "$wt_cache.new" \
                  && mv -f "$wt_cache.new" "$wt_cache"; then :; else rm -f "$wt_cache.new"; fi
        ) >/dev/null 2>&1 &
    fi
    if [ -s "$wt_cache" ]; then
        read -r live_agents total_agents land_live < "$wt_cache" 2>/dev/null
        live_agents=${live_agents:-0}; total_agents=${total_agents:-0}; land_live=${land_live:-0}
    fi
fi

# --- Fleet pipeline counts (A) ----------------------------------------------
# `bd list` costs ~0.85s; the statusline re-renders far too often to pay that
# synchronously, so read from a short-lived cache refreshed in the background.
# We cache ALL open issues (one call) and classify each into AT MOST ONE stage,
# under one invariant: no ticket is ever counted in two segments.
# The `stage` def below is a single if/elif ladder rather than five independent
# predicates plus a complement, so mutual exclusion is structural and costs
# LESS code, not more: `build:` is the ladder's fallthrough (claimed, handed
# off to nothing later), so there is no roster of sibling labels for it to
# exclude and nothing here to drift against /sweep SKILL.md §2b. The write side
# already keeps the siblings apart (/land swaps pipeline labels atomically,
# --remove-label X --add-label Y); the ladder means this renderer stays correct
# without depending on that. Zero-count stages are omitted.
#
# `--limit 0` is load-bearing, not noise. The canonical reason, the
# bd 1.1.0 measurements, and why this is HARDENING rather than a live fix all
# live in /sweep's SKILL.md, whose roster of pinned sites now lists
# this one -- 's audit missed it because `-C "$cwd"` sits between `bd`
# and `list`, defeating a literal 'bd list' grep, so a roster rather than a grep
# is what will find it next time. The latency note above is the one real
# objection to pinning here, and it is answered rather than overridden: the bd
# call is backgrounded and the foreground render only parses the cached JSON, so
# pinning moves no work onto the render path. Capping deliberately was the
# alternative, rejected because the trigger is already met -- the query below is
# unfiltered by status, so it already returns well past bd's documented default
# (63 rows when measured: 39 open + 18 in_progress + 6 deferred).
#
# Companion to /sweep's $HUMAN subtraction (same maintainer decision): a
# `human`-labeled ticket that is dependency-blocked is not decidable yet -- its
# sign-off artifact doesn't exist -- so it must not inflate `!human`. We cache
# `bd blocked --json`'s id set in a SECOND file, refreshed in the SAME detached
# background job as the `bd list` call above (one trigger, one TTL, no new
# synchronous call on the render path), and subtract it from the `human` arm
# only -- `land-escalated` always counts (escalations are never dependency-gated).
# `bd blocked` has no `--limit` flag to pin; it also cannot match the bd-list
# limit gate's regex, which requires a literal `list` token -- `blocked` doesn't
# contain one, so this call site sits outside that gate's scan surface by
# construction, not by omission.
#
# Failure semantics DELIBERATELY differ from /sweep's digest: this is a
# passive display, not a notification pipeline, so a missing/unreadable/invalid
# blocked cache fails OPEN -- count every `human` ticket, the prior behavior.
# Overcounting here is harmless (a stale-looking segment); silently hiding
# decidable work is not. No sentinel machinery needed -- an empty/absent
# `$blocked_cache` naturally yields an empty `$blocked_json` ("[]"), under which
# `isblocked` is false for every id, so the human arm counts everything.
# A STALE cache is the case the read side CANNOT catch (it is neither absent nor
# invalid), so the refresh job below deletes the cache whenever `bd blocked`
# fails -- without that, a persistently broken `bd blocked` would quietly hide
# decidable work indefinitely, the one failure this feature must never produce.
pipeline_part=""
if [ -n "$cwd" ] && [ -d "$cwd/.beads" ]; then
    cache="${TMPDIR:-/tmp}/-bd.cache"
    blocked_cache="${TMPDIR:-/tmp}/-blocked.cache"
    ttl=5
    now=$(date +%s)
    mtime=0
    [ -f "$cache" ] && mtime=$(stat -c %Y "$cache" 2>/dev/null || echo 0)
    if [ $((now - mtime)) -ge "$ttl" ]; then
        # Reset mtime first so the next few renders (within the ~0.85s bd takes)
        # don't each spawn their own refresh; then refresh detached. Both bd
        # calls run in this ONE background job, independently -- a failure in
        # either never blocks the other's cache from refreshing.
        touch "$cache"
        (
            if bd -C "$cwd" list --limit 0 --json 2>/dev/null > "$cache.new" \
                  && mv -f "$cache.new" "$cache"; then :; else rm -f "$cache.new"; fi
            # On failure drop the STALE cache too, not just the temp file --
            # otherwise a persistently broken `bd blocked` keeps subtracting a
            # frozen id set forever. This is the write-side half of the fail-open
            # contract in the design note above; deleting it fails open on the
            # very next render.
            if bd -C "$cwd" blocked --json 2>/dev/null > "$blocked_cache.new" \
                  && mv -f "$blocked_cache.new" "$blocked_cache"; then :; else
                rm -f "$blocked_cache.new" "$blocked_cache"; fi
        ) >/dev/null 2>&1 &
    fi
    if [ -s "$cache" ]; then
        # Fail open: absent/empty/invalid all collapse to "[]" (design note above).
        # The `case` is NOT redundant with a plain `|| blocked_json="[]"`, so
        # don't "simplify" it to one: on an EMPTY cache file jq exits 0 and
        # prints nothing, so the `||` never fires and the empty string reaches
        # `--argjson`, which rejects it and fails the WHOLE counts query -- every
        # segment disappears, not just `!human`. Guard the shape, not the exit.
        blocked_json=$(jq -c '[(. // [])[] | .id]' "$blocked_cache" 2>/dev/null)
        case "$blocked_json" in
            \[*\]) ;;
            *) blocked_json="[]" ;;
        esac
        counts=$(jq -r --argjson blocked "$blocked_json" '
            def hasl($l): ((.labels // []) | index($l)) != null;
            def isblocked: . as $t | ($blocked | index($t.id)) != null;
            # First matching arm wins; order IS the precedence rule. A ticket
            # waiting on a human outranks any pipeline label it still carries,
            # and the /sweep digest (permanently in_progress by design) maps to
            # no segment at all. Emitting nothing means "counted nowhere".
            # land-escalated always counts (never dependency-gated); a
            # dependency-blocked human ticket is excluded from `human` only.
            def stage:
                if   hasl("sweep-digest")                    then empty
                elif hasl("land-escalated")                  then "human"
                elif hasl("human") and (isblocked | not)     then "human"
                elif hasl("human")                           then empty
                elif hasl("needs-rebase")                    then "rebase"
                elif hasl("ready-for-land")                  then "land"
                elif hasl("ready-for-code-review")           then "review"
                elif .status == "in_progress"                then "build"
                else empty end;
            reduce (.[] | stage) as $s ({}; .[$s] += 1)
            | [ (.build // 0), (.review // 0), (.land // 0),
                (.rebase // 0), (.human // 0) ] | join(" ")
        ' "$cache" 2>/dev/null)
        if [ -n "$counts" ]; then
            read -r build review land rebase human <<< "$counts"
            # in_progress with no live agent backing it is stale -> mark "?".
            stale=""; [ "${build:-0}" -gt 0 ] && [ "${live_agents:-0}" -eq 0 ] && stale="?"
            segs=()
            [ "${build:-0}"  -gt 0 ] && segs+=("build:${build}${stale}")
            [ "${review:-0}" -gt 0 ] && segs+=("review:${review}")
            [ "${land:-0}"   -gt 0 ] && segs+=("land:${land}")
            [ "${rebase:-0}" -gt 0 ] && segs+=("rebase:${rebase}")
            [ "${human:-0}"  -gt 0 ] && segs+=("!human:${human}")
            [ ${#segs[@]} -gt 0 ] && pipeline_part="${segs[*]}"
        fi
    fi
fi

# --- Live agents segment (rendered) -----------------------------------------
agents_part=""
if [ "${total_agents:-0}" -gt 0 ]; then
    agents_part="agents:${live_agents}"
    [ "${land_live:-0}" -gt 0 ] && agents_part="${agents_part} (${land_live} land)"
    idle=$((total_agents - live_agents))
    [ "$idle" -gt 0 ] && agents_part="${agents_part}, ${idle} stale"
fi

# --- Git (fleet-aware short form) -------------------------------------------
# Beyond branch + dirty count, show how far this branch sits from its merge
# TARGET — that's the git-side analogue of the bd pipeline view. On main the
# target is origin/main; on a feature/worktree branch it's local main.
git_part=""
if [ -n "$cwd" ] && git -C "$cwd" rev-parse --git-dir > /dev/null 2>&1; then
    branch=$(git -C "$cwd" symbolic-ref --short HEAD 2>/dev/null || git -C "$cwd" rev-parse --short HEAD 2>/dev/null)
    status=$(git -C "$cwd" status --porcelain 2>/dev/null)
    if [ -z "$status" ]; then
        git_status="clean"
    else
        changed=$(echo "$status" | grep -vc '^??')
        untracked=$(echo "$status" | grep -c '^??')
        git_status=""
        [ "$changed" -gt 0 ] && git_status="${changed}~"
        [ "$untracked" -gt 0 ] && git_status="${git_status}${untracked}?"
    fi

    # Divergence from the merge target (↑ahead ↓behind), zeros omitted.
    if [ "$branch" = "main" ]; then base="origin/main"; else base="main"; fi
    div=""
    if [ "$branch" != "$base" ] && git -C "$cwd" rev-parse --verify -q "$base" >/dev/null 2>&1; then
        ahead=$(git -C "$cwd" rev-list --count "${base}..HEAD" 2>/dev/null || echo 0)
        behind=$(git -C "$cwd" rev-list --count "HEAD..${base}" 2>/dev/null || echo 0)
        [ "${ahead:-0}" -gt 0 ] && div="↑${ahead}"
        [ "${behind:-0}" -gt 0 ] && div="${div:+$div }↓${behind}"
    fi

    git_part="git:${branch} ${git_status}"
    [ -n "$div" ] && git_part="${git_part} ${div}"
fi

# --- Model + usage meters ---------------------------------------------------
# Model: only the class matters (Opus/Sonnet/…), so keep the first word and drop
# the version + "(1M context)" tail that display_name carries.
model_part=""
[ -n "$model" ] && model_part="${model%% *}"

# Colour-coded usage meters. Both the 5h window and the context meter render as a
# green->red gradient bar (the fill reaches further into the red the fuller it is)
# with the percent number coloured at the current level, so fullness reads at a
# glance; the context meter carries a /compact hint once it crosses COMPACT_THRESHOLD.
RESET=$(printf '\033[0m')
DIM=$(printf '\033[38;2;90;90;90m')   # unfilled cells

# ANSI truecolor escape for a green->yellow->red heat colour at integer pct 0..100.
heat_color() {
    pct=$1
    [ "$pct" -lt 0 ] && pct=0; [ "$pct" -gt 100 ] && pct=100
    if [ "$pct" -lt 50 ]; then          # green (60,200,60) -> yellow (220,200,0)
        r=$(( 60 + pct * (220 - 60) / 50 )); g=200; b=$(( 60 - pct * 60 / 50 ))
    else                                # yellow (220,200,0) -> red (220,40,40)
        t=$(( pct - 50 )); r=220; g=$(( 200 - t * (200 - 40) / 50 )); b=$(( t * 40 / 50 ))
    fi
    printf '\033[38;2;%d;%d;%dm' "$r" "$g" "$b"
}

# A 10-cell heat bar for an integer percent 0..100. The bar is a fixed green->red
# GRADIENT across its width — cell 0 green ... cell 9 red — and fill lights cells
# left to right, so a fuller bar reaches further into the red. Each lit cell wears
# its own position colour; unfilled cells are dim: ████░░░░░░ (green→…→dim).
make_bar() {
    pct=$1; width=10
    [ "$pct" -lt 0 ] && pct=0; [ "$pct" -gt 100 ] && pct=100
    filled=$(( pct * width / 100 ))
    out=""; i=0
    while [ "$i" -lt "$width" ]; do
        if [ "$i" -lt "$filled" ]; then
            out="${out}$(heat_color $(( (i * 100 + 50) / width )))█"   # cell's own gradient colour
        else
            out="${out}${DIM}░"
        fi
        i=$((i + 1))
    done
    printf '%s%s' "$out" "$RESET"
}

# Compact "time remaining" until a Unix-epoch instant: "2h05m", "45m", or empty
# if the instant is absent, non-numeric, or already past. Tolerates epoch given
# in seconds or milliseconds.
countdown() {
    ts=$1
    [ -z "$ts" ] && return
    ts=$(printf '%.0f' "$ts" 2>/dev/null) || return
    case "$ts" in ''|*[!0-9]*) return ;; esac
    [ "$ts" -gt 100000000000 ] && ts=$(( ts / 1000 ))   # epoch ms -> s
    rem=$(( ts - $(date +%s) ))
    [ "$rem" -le 0 ] && return
    h=$(( rem / 3600 )); m=$(( (rem % 3600) / 60 ))
    if [ "$h" -gt 0 ]; then printf '%dh%02dm' "$h" "$m"; else printf '%dm' "$m"; fi
}

# Usage meters: the 5-hour "current session" rate-limit window (matching /usage)
# and a context-window occupancy meter. The 5h window is only present for
# Claude.ai subscribers after the first API response — omit it when absent; when
# present it carries a compact "↻" countdown to when the window rolls over
# (rate_limits.five_hour.resets_at), shown only when that reset instant is given.
# The context meter (labeled "ctx", tokens used / window size) is always shown when
# context data is present, so the line is never blank early in a session.
#
# COMPACT_THRESHOLD: once context occupancy reaches this percent, the ctx meter
# appends a red "⚠ /compact" hint — a nudge to compact before auto-compaction /
# a hard context limit forces it. Tune to taste.
COMPACT_THRESHOLD=80
usage_parts=()
sess=$(echo "$input" | jq -r '.rate_limits.five_hour.used_percentage // empty')
if [ -n "$sess" ]; then
    p=$(printf '%.0f' "$sess"); col=$(heat_color "$p")
    meter="5h $(make_bar "$p") ${col}${p}%${RESET}"
    # Countdown to when the 5h window rolls over, if the reset instant is present.
    reset=$(echo "$input" | jq -r '.rate_limits.five_hour.resets_at // empty')
    cd=$(countdown "$reset")
    [ -n "$cd" ] && meter="${meter} ↻ ${cd}"
    usage_parts+=("$meter")
fi
u=""
if [ -n "$used_tokens" ] && [ -n "$window" ] && [ "$window" -gt 0 ] 2>/dev/null; then
    u=$(( used_tokens * 100 / window ))
elif [ -n "$used" ]; then
    u=$(printf '%.0f' "$used")
fi
if [ -n "$u" ]; then
    col=$(heat_color "$u")
    ctx_part="ctx $(make_bar "$u") ${col}${u}%${RESET}"
    if [ "$u" -ge "$COMPACT_THRESHOLD" ]; then
        ctx_part="${ctx_part} $(heat_color 100)⚠ /compact${RESET}"
    fi
    usage_parts+=("$ctx_part")
fi

# --- Assemble (fleet first), skipping empties -------------------------------
parts=()
[ -n "$pipeline_part" ] && parts+=("$pipeline_part")
[ -n "$agents_part" ] && parts+=("$agents_part")
[ -n "$git_part" ] && parts+=("$git_part")
[ -n "$model_part" ] && parts+=("$model_part")
for up in "${usage_parts[@]}"; do parts+=("$up"); done

# Join with an explicit " | " (IFS+"${arr[*]}" would join on a single space,
# blurring the pipeline group into the agents group).
sep=""; out=""
for p in "${parts[@]}"; do out="${out}${sep}${p}"; sep=" | "; done
printf '%s' "$out"
