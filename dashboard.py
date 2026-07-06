"""
dashboard.py - Local web dashboard served on localhost:8080.
"""

import json
import os
import sqlite3
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from pathlib import Path
from datetime import datetime

from scanner import VERSION, init_db

DB_PATH = Path(os.environ.get("CLAUDE_USAGE_DB", Path.home() / ".claude" / "usage.db"))

# Which surface is rendering the dashboard: "web" (standalone `cli.py dashboard`)
# or "vscode" (embedded in the extension's sidebar webview). serve() sets this
# from the --surface flag the extension passes. The footer reads it to decide
# what to show — the web build promotes the VS Code extension and offers a
# "check GitHub for a newer release" update link; the embedded build shows just
# the version (VS Code updates the extension itself, and a GitHub-release check
# would misfire there because the Marketplace publish lags the GitHub release).
SURFACE = "web"


def get_dashboard_data(db_path=DB_PATH):
    if not db_path.exists():
        return {"error": "Database not found. Run: python cli.py scan"}

    conn = sqlite3.connect(db_path)
    # The dashboard reads while a background scan may be committing (cmd_dashboard
    # serves first, scans in a background thread; /api/rescan scans in-process too).
    # Wait briefly for write locks instead of raising "database is locked".
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.row_factory = sqlite3.Row
    # Ensure the schema is current before querying. cmd_dashboard binds and serves
    # *before* its background scan runs init_db, so on the first load after an
    # upgrade a pre-existing DB may still be on the old schema — the subagent
    # queries below reference the `agents` table and the `is_subagent`/`agent_id`
    # columns and would raise "no such table: agents" until the scan caught up.
    # init_db is idempotent (CREATE ... IF NOT EXISTS + additive column checks),
    # so this is a cheap no-op once migrated.
    init_db(conn)

    # Every below-the-fold dataset joins sessions for project_name, since the
    # Projects multi-select filter needs to narrow all of them, same as models.
    PROJECT_EXPR = "COALESCE(NULLIF(s.project_name, ''), 'unknown')"

    # ── All models / projects (for filter UI) ─────────────────────────────────
    # GROUP BY uses the normalised expression too so NULL and '' don't end up
    # as two separate "unknown" rows.
    model_rows = conn.execute("""
        SELECT COALESCE(NULLIF(model, ''), 'unknown') as model
        FROM turns
        GROUP BY COALESCE(NULLIF(model, ''), 'unknown')
        ORDER BY SUM(input_tokens + output_tokens) DESC
    """).fetchall()
    all_models = [r["model"] for r in model_rows]

    project_rows = conn.execute("""
        SELECT COALESCE(NULLIF(project_name, ''), 'unknown') as project
        FROM sessions
        GROUP BY COALESCE(NULLIF(project_name, ''), 'unknown')
        ORDER BY project COLLATE NOCASE
    """).fetchall()
    all_projects = [r["project"] for r in project_rows]

    # ── Daily per-model per-project, ALL history (client filters by range) ────
    daily_rows = conn.execute(f"""
        SELECT
            substr(t.timestamp, 1, 10)                as day,
            COALESCE(NULLIF(t.model, ''), 'unknown')  as model,
            {PROJECT_EXPR}                             as project,
            SUM(t.input_tokens)                       as input,
            SUM(t.output_tokens)                      as output,
            SUM(t.cache_read_tokens)                  as cache_read,
            SUM(t.cache_creation_tokens)               as cache_creation,
            COUNT(*)                                  as turns
        FROM turns t
        LEFT JOIN sessions s ON s.session_id = t.session_id
        GROUP BY day, COALESCE(NULLIF(t.model, ''), 'unknown'), project
        ORDER BY day, model
    """).fetchall()

    daily_by_model = [{
        "day":            r["day"],
        "model":          r["model"],
        "project":        r["project"],
        "input":          r["input"] or 0,
        "output":         r["output"] or 0,
        "cache_read":     r["cache_read"] or 0,
        "cache_creation": r["cache_creation"] or 0,
        "turns":          r["turns"] or 0,
    } for r in daily_rows]

    # ── Hourly per-day per-model per-project (client filters by range + TZ) ───
    # Timestamps are ISO8601 UTC (e.g. "2026-04-08T09:30:00Z"); chars 12-13 = hour.
    # Carries full input/output/cache sums (not just output) so the below-the-fold
    # By Model / Cost by Model views can also use this as their hour-resolution
    # source when an hour/minute Timeline selection is narrowing the page.
    hourly_rows = conn.execute(f"""
        SELECT
            substr(t.timestamp, 1, 10)                as day,
            CAST(substr(t.timestamp, 12, 2) AS INTEGER) as hour,
            COALESCE(NULLIF(t.model, ''), 'unknown')  as model,
            {PROJECT_EXPR}                             as project,
            SUM(t.input_tokens)                       as input,
            SUM(t.output_tokens)                      as output,
            SUM(t.cache_read_tokens)                  as cache_read,
            SUM(t.cache_creation_tokens)               as cache_creation,
            COUNT(*)                                  as turns
        FROM turns t
        LEFT JOIN sessions s ON s.session_id = t.session_id
        WHERE t.timestamp IS NOT NULL AND length(t.timestamp) >= 13
        GROUP BY day, hour, COALESCE(NULLIF(t.model, ''), 'unknown'), project
        ORDER BY day, hour, model
    """).fetchall()

    hourly_by_model = [{
        "day":    r["day"],
        "hour":   r["hour"] if r["hour"] is not None else 0,
        "model":  r["model"],
        "project":        r["project"],
        "input":          r["input"] or 0,
        "output": r["output"] or 0,
        "cache_read":     r["cache_read"] or 0,
        "cache_creation": r["cache_creation"] or 0,
        "turns":  r["turns"] or 0,
    } for r in hourly_rows]

    # ── All sessions (client filters by range and model) ──────────────────────
    session_rows = conn.execute("""
        SELECT
            session_id, project_name, first_timestamp, last_timestamp,
            total_input_tokens, total_output_tokens,
            total_cache_read, total_cache_creation, model, turn_count,
            git_branch, topic
        FROM sessions
        ORDER BY last_timestamp DESC
    """).fetchall()

    sessions_all = []
    for r in session_rows:
        try:
            t1 = datetime.fromisoformat(r["first_timestamp"].replace("Z", "+00:00"))
            t2 = datetime.fromisoformat(r["last_timestamp"].replace("Z", "+00:00"))
            duration_min = round((t2 - t1).total_seconds() / 60, 1)
        except Exception:
            duration_min = 0
        sessions_all.append({
            # Full id: the table truncates for display, but the CSV export
            # needs the whole thing (an 8-char prefix isn't uniquely useful).
            "session_id":    r["session_id"],
            "project":       r["project_name"] or "unknown",
            "branch":        r["git_branch"] or "",
            "topic":         r["topic"] or "",
            "last":          (r["last_timestamp"] or "")[:16].replace("T", " "),
            "last_date":     (r["last_timestamp"] or "")[:10],
            "duration_min":  duration_min,
            "model":         r["model"] or "unknown",
            "turns":         r["turn_count"] or 0,
            "input":         r["total_input_tokens"] or 0,
            "output":        r["total_output_tokens"] or 0,
            "cache_read":    r["total_cache_read"] or 0,
            "cache_creation": r["total_cache_creation"] or 0,
        })

    # ── Subagent breakdown by type, by day & model ────────────────────────────
    # JOIN turns to agents (parent tool_result metadata captured by the scanner).
    # acompact-* ids are Claude Code's auto-compaction subagent (no parent
    # dispatch record); anything else without a match is shown as 'unknown'.
    AGENT_TYPE_EXPR = (
        "COALESCE(a.agent_type, "
        "CASE WHEN t.agent_id LIKE 'acompact-%' THEN 'auto-compact' "
        "ELSE 'unknown' END)"
    )

    # Project comes from the dispatching *parent* session (agents.dispatched_in_session),
    # not t.session_id — the subagent's own transcript session isn't guaranteed to
    # carry the same project_name mapping.
    subagent_daily_rows = conn.execute(f"""
        SELECT
            substr(t.timestamp, 1, 10)               as day,
            CAST(substr(t.timestamp, 12, 2) AS INTEGER) as hour,
            {AGENT_TYPE_EXPR}                        as agent_type,
            COALESCE(NULLIF(t.model, ''), 'unknown') as model,
            COALESCE(NULLIF(s.project_name, ''), 'unknown') as project,
            SUM(t.input_tokens)                      as input,
            SUM(t.output_tokens)                     as output,
            SUM(t.cache_read_tokens)                 as cache_read,
            SUM(t.cache_creation_tokens)             as cache_creation,
            COUNT(DISTINCT t.agent_id)               as dispatches,
            COUNT(*)                                 as turns
        FROM turns t
        LEFT JOIN agents a ON t.agent_id = a.agent_id
        LEFT JOIN sessions s ON s.session_id = a.dispatched_in_session
        WHERE t.is_subagent = 1
        GROUP BY day, hour, agent_type, COALESCE(NULLIF(t.model, ''), 'unknown'), project
        ORDER BY day, agent_type
    """).fetchall()

    subagent_by_type = [{
        "day":            r["day"],
        "hour":           r["hour"] if r["hour"] is not None else 0,
        "agent_type":     r["agent_type"],
        "model":          r["model"],
        "project":        r["project"],
        "input":          r["input"] or 0,
        "output":         r["output"] or 0,
        "cache_read":     r["cache_read"] or 0,
        "cache_creation": r["cache_creation"] or 0,
        "dispatches":     r["dispatches"] or 0,
        "turns":          r["turns"] or 0,
    } for r in subagent_daily_rows]

    # ── Top individual subagent dispatches (one row per agent_id) ─────────────
    top_dispatch_rows = conn.execute(f"""
        SELECT
            t.agent_id                               as agent_id,
            {AGENT_TYPE_EXPR}                        as agent_type,
            COALESCE(NULLIF(t.model, ''), 'unknown') as model,
            COALESCE(NULLIF(s.project_name, ''), 'unknown') as project,
            MIN(t.timestamp)                         as start_ts,
            SUM(t.input_tokens)                      as input,
            SUM(t.output_tokens)                     as output,
            SUM(t.cache_read_tokens)                 as cache_read,
            SUM(t.cache_creation_tokens)             as cache_creation,
            COUNT(*)                                 as turns,
            a.dispatched_in_session                  as parent_session,
            a.total_duration_ms                      as duration_ms,
            a.tool_use_count                         as tool_uses,
            a.status                                 as status
        FROM turns t
        LEFT JOIN agents a ON t.agent_id = a.agent_id
        LEFT JOIN sessions s ON s.session_id = a.dispatched_in_session
        WHERE t.is_subagent = 1 AND t.agent_id IS NOT NULL
        GROUP BY t.agent_id
        ORDER BY (SUM(t.input_tokens) + SUM(t.output_tokens)
                  + SUM(t.cache_read_tokens) + SUM(t.cache_creation_tokens)) DESC
    """).fetchall()

    top_dispatches = [{
        "agent_id":       r["agent_id"],
        "agent_type":     r["agent_type"],
        "model":          r["model"],
        "project":        r["project"],
        "start":          (r["start_ts"] or "")[:16].replace("T", " "),
        "start_date":     (r["start_ts"] or "")[:10],
        "input":          r["input"] or 0,
        "output":         r["output"] or 0,
        "cache_read":     r["cache_read"] or 0,
        "cache_creation": r["cache_creation"] or 0,
        "turns":          r["turns"] or 0,
        "duration_ms":    r["duration_ms"],
        "tool_uses":      r["tool_uses"],
        "status":         r["status"],
    } for r in top_dispatch_rows]

    # ── Skill usage, by day/hour, model & project ─────────────────────────────
    # skill_name is only populated for tool_name='Skill' turns (see scanner.py) —
    # every other turn has it NULL and is excluded here.
    skill_daily_rows = conn.execute(f"""
        SELECT
            substr(t.timestamp, 1, 10)               as day,
            CAST(substr(t.timestamp, 12, 2) AS INTEGER) as hour,
            t.skill_name                              as skill,
            COALESCE(NULLIF(t.model, ''), 'unknown')  as model,
            {PROJECT_EXPR}                             as project,
            SUM(t.input_tokens)                       as input,
            SUM(t.output_tokens)                      as output,
            SUM(t.cache_read_tokens)                  as cache_read,
            SUM(t.cache_creation_tokens)               as cache_creation,
            COUNT(*)                                  as turns
        FROM turns t
        LEFT JOIN sessions s ON s.session_id = t.session_id
        WHERE t.skill_name IS NOT NULL AND t.skill_name != ''
        GROUP BY day, hour, skill, COALESCE(NULLIF(t.model, ''), 'unknown'), project
        ORDER BY day, skill
    """).fetchall()

    skill_by_day = [{
        "day":            r["day"],
        "hour":           r["hour"] if r["hour"] is not None else 0,
        "skill":          r["skill"],
        "model":          r["model"],
        "project":        r["project"],
        "input":          r["input"] or 0,
        "output":         r["output"] or 0,
        "cache_read":     r["cache_read"] or 0,
        "cache_creation": r["cache_creation"] or 0,
        "turns":          r["turns"] or 0,
    } for r in skill_daily_rows]

    # ── Tool usage, by day/hour, model & project ──────────────────────────────
    # Raw tool_name per row (e.g. "Bash", "mcp__playwright__browser_click") —
    # the client collapses "mcp__<server>__<tool>" down to its MCP server, and
    # "Bash" + cli_name down to "CLI: <name>" (see toolGroupLabel), for the
    # "Token Usage by MCP / CLI" table. cli_name is only populated for
    # tool_name='Bash' turns (the command's first word — see scanner.py).
    tool_daily_rows = conn.execute(f"""
        SELECT
            substr(t.timestamp, 1, 10)               as day,
            CAST(substr(t.timestamp, 12, 2) AS INTEGER) as hour,
            t.tool_name                               as tool_name,
            t.cli_name                                as cli_name,
            COALESCE(NULLIF(t.model, ''), 'unknown')  as model,
            {PROJECT_EXPR}                             as project,
            SUM(t.input_tokens)                       as input,
            SUM(t.output_tokens)                      as output,
            SUM(t.cache_read_tokens)                  as cache_read,
            SUM(t.cache_creation_tokens)               as cache_creation,
            COUNT(*)                                  as turns
        FROM turns t
        LEFT JOIN sessions s ON s.session_id = t.session_id
        WHERE t.tool_name IS NOT NULL AND t.tool_name != ''
        GROUP BY day, hour, tool_name, cli_name, COALESCE(NULLIF(t.model, ''), 'unknown'), project
        ORDER BY day, tool_name
    """).fetchall()

    tool_by_day = [{
        "day":            r["day"],
        "hour":           r["hour"] if r["hour"] is not None else 0,
        "tool_name":      r["tool_name"],
        "cli_name":       r["cli_name"],
        "model":          r["model"],
        "project":        r["project"],
        "input":          r["input"] or 0,
        "output":         r["output"] or 0,
        "cache_read":     r["cache_read"] or 0,
        "cache_creation": r["cache_creation"] or 0,
        "turns":          r["turns"] or 0,
    } for r in tool_daily_rows]

    conn.close()

    return {
        "all_models":      all_models,
        "all_projects":    all_projects,
        "daily_by_model":  daily_by_model,
        "hourly_by_model": hourly_by_model,
        "sessions_all":    sessions_all,
        "subagent_by_type": subagent_by_type,
        "top_dispatches":  top_dispatches,
        "skill_by_day":    skill_by_day,
        "tool_by_day":     tool_by_day,
        "generated_at":    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


# strftime formats for the "Usage Timeline" chart's bucket resolution. Kept as
# a fixed whitelist (never built from request input) so the granularity param
# can't be used to smuggle an arbitrary format string into the query.
TIMELINE_FORMATS = {
    "day":    "%Y-%m-%d",
    "minute": "%Y-%m-%d %H:%M",
    "hour":   "%Y-%m-%d %H:00",
}

# Unlike daily_by_model/hourly_by_model above (which ship all history and let
# the client filter by range), minute buckets over months of history would be
# a huge payload — so minute granularity requires a bounded [start, end] no
# wider than this many days. The UI only ever offers minute view for Today/
# Yesterday (single-day ranges), but this is enforced here too, not just in
# the UI, since the endpoint is a plain GET anyone could hit directly.
TIMELINE_MINUTE_MAX_DAYS = 1


def get_timeline_data(db_path=DB_PATH, granularity="hour", start=None, end=None):
    """Per-bucket, per-project, per-model token usage for the Usage Timeline chart.

    Queried fresh per request (scoped to start/end) rather than joining the
    all-history blob get_dashboard_data returns, since minute/hour buckets
    over unbounded history don't fit the "ship everything, filter on the
    client" pattern the daily/hourly charts use.
    """
    if granularity not in TIMELINE_FORMATS:
        return {"error": "invalid granularity: %r (expected 'day', 'hour', or 'minute')" % granularity}

    if granularity == "minute":
        if not start or not end:
            return {"error": "minute granularity requires a bounded range (e.g. Today or This Week)"}
        try:
            span_days = (datetime.fromisoformat(end) - datetime.fromisoformat(start)).days
        except ValueError:
            return {"error": "invalid start/end date"}
        if span_days > TIMELINE_MINUTE_MAX_DAYS:
            return {"error": "minute granularity is limited to a %d-day window" % TIMELINE_MINUTE_MAX_DAYS}

    if not db_path.exists():
        return {"error": "Database not found. Run: python cli.py scan"}

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.row_factory = sqlite3.Row
    init_db(conn)

    # Matches the daily_by_model/hourly_by_model convention of comparing the
    # YYYY-MM-DD date prefix rather than full timestamps, so start/end from
    # the client's getRangeBounds() (plain date strings) behave identically
    # here as they do for those charts.
    rows = conn.execute("""
        SELECT
            strftime(?, t.timestamp)                        as bucket,
            COALESCE(NULLIF(s.project_name, ''), 'unknown') as project,
            COALESCE(NULLIF(t.model, ''), 'unknown')        as model,
            SUM(t.input_tokens)                             as input,
            SUM(t.output_tokens)                            as output,
            SUM(t.cache_read_tokens)                        as cache_read,
            SUM(t.cache_creation_tokens)                     as cache_creation,
            COUNT(*)                                        as turns
        FROM turns t
        LEFT JOIN sessions s ON s.session_id = t.session_id
        WHERE t.timestamp IS NOT NULL AND length(t.timestamp) >= 13
          AND (? IS NULL OR substr(t.timestamp, 1, 10) >= ?)
          AND (? IS NULL OR substr(t.timestamp, 1, 10) <= ?)
        GROUP BY bucket, project, COALESCE(NULLIF(t.model, ''), 'unknown')
        ORDER BY bucket
    """, (TIMELINE_FORMATS[granularity], start, start, end, end)).fetchall()
    conn.close()

    return {
        "granularity": granularity,
        "rows": [{
            "bucket":         r["bucket"],
            "project":        r["project"],
            "model":          r["model"],
            "input":          r["input"] or 0,
            "output":         r["output"] or 0,
            "cache_read":     r["cache_read"] or 0,
            "cache_creation": r["cache_creation"] or 0,
            "turns":          r["turns"] or 0,
        } for r in rows],
    }


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Claude Code Usage Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-zoom@2.0.1/dist/chartjs-plugin-zoom.min.js"></script>
<script>window.APP_CONFIG = __APP_CONFIG_JSON__;</script>
<style>
  :root {
    --bg: #161617;      /* page base */
    --card: #1E1F20;    /* raised one step above the page */
    --border: #2C2D2E;
    --text: #BFBFBF;
    --muted: #4F4F50;
    --accent: #d97757;
    --blue: #48A0C7;
    --green: #74C991;
    --red: #C74E39;
    --raised: #2E2F31;  /* hover / raised surfaces — top of the elevation ladder */
    --selected: #262626;  /* selected chips / tabs (neutral, not accent) */
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; font-size: 14px; }

  /* VS Code-style scrollbars. The dashboard renders inside a webview iframe,
     which doesn't inherit VS Code's --vscode-* theme variables, so we set the
     scrollbar here: no arrows, grey thumb (#28292B, #8B8B8D on hover) over a
     #121314 track, in a 21px gutter. Also fits the dark UI standalone. */
  * { scrollbar-width: auto; scrollbar-color: #28292B #121314; }
  ::-webkit-scrollbar { width: 21px; height: 21px; }
  ::-webkit-scrollbar-track { background: #121314; }
  ::-webkit-scrollbar-thumb { background-color: #28292B; border: 3px solid transparent; background-clip: padding-box; }
  ::-webkit-scrollbar-thumb:hover { background-color: #8B8B8D; }
  ::-webkit-scrollbar-thumb:active { background-color: #8B8B8D; }
  ::-webkit-scrollbar-corner { background: #121314; }

  header { background: var(--card); border-bottom: 1px solid var(--border); padding: 16px 24px; display: flex; align-items: center; justify-content: space-between; }
  header h1 { font-size: 18px; font-weight: 600; color: var(--text); }
  header .header-title { display: flex; align-items: center; gap: 10px; }
  /* The icon is a monochrome silhouette (white shape on transparent). We paint
     it with the title color via a CSS mask + background-color, so it matches
     `header h1` — the lightest text color. */
  header .header-icon {
    width: 26px; height: 26px; flex-shrink: 0; display: block;
    background-color: var(--text);
    -webkit-mask: url("icon.svg") no-repeat center / contain;
    mask: url("icon.svg") no-repeat center / contain;
  }
  header .meta { color: var(--muted); font-size: 12px; text-align: right; line-height: 1.5; margin-right: 20px; }
  #rescan-btn { background: var(--card); border: 1px solid var(--border); color: var(--muted); padding: 4px 12px; border-radius: 6px; cursor: pointer; font-size: 12px; margin-top: 4px; }
  #rescan-btn:hover { color: var(--text); border-color: var(--accent); }
  #rescan-btn:disabled { opacity: 0.5; cursor: not-allowed; }

  /* No longer a full-bleed sticky top bar — it lives between the fixed 30-day
     overview and the filterable sections, so it reads as a card like the rest. */
  #filter-bar { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 14px 16px; display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-bottom: 20px; }
  .filter-label { font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); white-space: nowrap; }
  .filter-sep { width: 1px; height: 22px; background: var(--border); flex-shrink: 0; }
  .filter-hint { font-size: 11px; color: var(--muted); width: 100%; margin-top: 2px; }
  /* Model multi-select: a compact trigger in the bar that opens a grouped panel. */
  .model-select { position: relative; flex-shrink: 0; }
  .model-trigger { display: flex; align-items: center; gap: 8px; min-width: 170px; max-width: 320px; padding: 5px 10px; background: var(--card); border: 1px solid var(--border); border-radius: 6px; color: var(--text); font-size: 12px; cursor: pointer; transition: border-color 0.15s; }
  .model-trigger:hover, .model-trigger.open { border-color: var(--accent); }
  #model-trigger-label { flex: 1; text-align: left; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .model-caret { color: var(--muted); font-size: 10px; flex-shrink: 0; transition: transform 0.15s; }
  .model-trigger.open .model-caret { transform: rotate(180deg); }
  .model-panel { position: absolute; top: calc(100% + 6px); left: 0; z-index: 50; min-width: 250px; max-width: 340px; max-height: 360px; overflow-y: auto; background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 8px; box-shadow: 0 8px 24px rgba(0,0,0,0.35); }
  .model-panel[hidden] { display: none; }
  .model-panel-actions { display: flex; gap: 6px; padding-bottom: 8px; margin-bottom: 4px; border-bottom: 1px solid var(--border); }
  .model-group-label { font-size: 10px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); padding: 8px 8px 4px; }
  .model-cb-label { display: flex; align-items: center; gap: 8px; padding: 6px 8px; border-radius: 6px; cursor: pointer; font-size: 12px; color: var(--muted); transition: background 0.12s, color 0.12s; user-select: none; }
  .model-cb-label:hover { background: var(--raised); color: var(--text); }
  .model-cb-label.checked { color: var(--text); }
  .model-cb-label input { display: none; }
  .model-cb-box { width: 15px; height: 15px; flex-shrink: 0; border-radius: 4px; border: 1px solid var(--border); display: flex; align-items: center; justify-content: center; font-size: 10px; line-height: 1; color: transparent; transition: background 0.12s, border-color 0.12s; }
  .model-cb-label.checked .model-cb-box { background: var(--accent); border-color: var(--accent); color: #fff; }
  .model-cb-text { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .filter-btn { padding: 3px 10px; border-radius: 4px; border: 1px solid var(--border); background: transparent; color: var(--muted); font-size: 11px; cursor: pointer; white-space: nowrap; }
  .filter-btn:hover { border-color: var(--accent); color: var(--text); }
  /* Date range — a compact dropdown. The old segmented button row (8 buttons)
     wrapped badly in the narrow VS Code panel; a single select stays put. Styled
     to match the model trigger. */
  .range-select { position: relative; flex-shrink: 0; }
  .range-select select { appearance: none; -webkit-appearance: none; min-width: 150px; padding: 5px 30px 5px 10px; background: var(--card); border: 1px solid var(--border); border-radius: 6px; color: var(--text); font-size: 12px; cursor: pointer; transition: border-color 0.15s; }
  .range-select select:hover, .range-select select:focus { border-color: var(--accent); outline: none; }
  .range-select::after { content: "\25BE"; position: absolute; right: 11px; top: 50%; transform: translateY(-50%); color: var(--muted); font-size: 10px; pointer-events: none; }
  .range-select option { background: var(--card); color: var(--text); }

  .container { max-width: 1400px; margin: 0 auto; padding: 24px; }
  .stats-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 16px; margin-bottom: 24px; }
  .stat-card { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 16px; }
  .stat-card .label { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 6px; }
  .stat-card .value { font-size: 22px; font-weight: 700; }
  .stat-card .sub { color: var(--muted); font-size: 11px; margin-top: 4px; }

  .charts-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 24px; }
  /* min-width:0 lets the grid column shrink below the canvas's intrinsic
     pixel width; without it, narrowing the window can't narrow the container,
     so Chart.js's ResizeObserver never fires until a data refresh rebuilds the
     canvas. (Expanding already works — 1fr columns grow freely.) */
  .chart-card { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 20px; min-width: 0; }
  .chart-card.wide { grid-column: 1 / -1; }
  .chart-card h2 { font-size: 13px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 16px; }
  .chart-wrap { position: relative; height: 240px; }
  .chart-wrap.tall { height: 300px; }
  .chart-header { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px; margin-bottom: 16px; }
  .chart-header h2 { margin-bottom: 0; }
  .chart-header-right { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
  .chart-day-count { font-size: 11px; color: var(--muted); }
  .tz-group { display: flex; border: 1px solid var(--border); border-radius: 6px; overflow: hidden; }
  .tz-btn { padding: 3px 10px; background: transparent; border: none; border-right: 1px solid var(--border); color: var(--muted); font-size: 11px; cursor: pointer; transition: background 0.15s, color 0.15s; text-transform: uppercase; letter-spacing: 0.05em; font-weight: 600; }
  .tz-btn:last-child { border-right: none; }
  .tz-btn:hover { background: var(--raised); color: var(--text); }
  .tz-btn.active { background: var(--selected); color: var(--text); }
  .tz-btn:disabled { opacity: 0.4; cursor: not-allowed; }
  .timeline-body { display: flex; gap: 16px; }
  .timeline-chart-wrap { flex: 1 1 70%; min-width: 0; }
  .timeline-side { flex: 0 0 220px; max-height: 300px; overflow-y: auto; }
  .timeline-side-title { font-size: 11px; color: var(--muted); margin-bottom: 8px; text-transform: uppercase; letter-spacing: 0.05em; font-weight: 600; }
  .timeline-side-table { width: 100%; font-size: 12px; border-collapse: collapse; }
  .timeline-side-table th, .timeline-side-table td { padding: 4px 6px; text-align: right; border-bottom: 1px solid var(--border); white-space: nowrap; }
  .timeline-side-table th:first-child, .timeline-side-table td:first-child { text-align: left; overflow: hidden; text-overflow: ellipsis; max-width: 120px; }
  .timeline-side-table th { color: var(--muted); font-weight: 600; }
  .timeline-hint { font-size: 11px; color: var(--muted); margin: 10px 0 0; }
  @media (max-width: 768px) { .timeline-body { flex-direction: column; } .timeline-side { flex: 0 0 auto; max-height: none; } }
  .peak-legend { display: inline-flex; align-items: center; gap: 5px; font-size: 11px; color: var(--muted); }
  .peak-swatch { width: 10px; height: 10px; background: var(--red); border-radius: 2px; display: inline-block; }

  table { width: 100%; border-collapse: collapse; }
  th { text-align: left; padding: 8px 12px; font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); border-bottom: 1px solid var(--border); white-space: nowrap; }
  th.sortable { cursor: pointer; user-select: none; }
  th.sortable:hover { color: var(--text); }
  .sort-icon { font-size: 9px; opacity: 0.8; }
  td { padding: 10px 12px; border-bottom: 1px solid var(--border); font-size: 13px; }
  tr:last-child td { border-bottom: none; }
  tr:hover td { background: var(--raised); }
  .model-tag { display: inline-block; padding: 2px 7px; border-radius: 4px; font-size: 11px; background: rgba(72,160,199,0.15); color: var(--blue); }
  .cost { color: var(--green); font-family: monospace; }
  .cost-na { color: var(--muted); font-family: monospace; font-size: 11px; }
  .num { font-family: monospace; }
  .muted { color: var(--muted); }
  .topic-cell { box-sizing: border-box; min-width: 160px; max-width: 260px; overflow-wrap: anywhere; font-size: 12px; color: var(--text); }
  .untitled { color: var(--muted); font-style: italic; }
  .section-title { font-size: 13px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 12px; }
  .section-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
  .section-header .section-title { margin-bottom: 0; }
  .export-btn { background: var(--card); border: 1px solid var(--border); color: var(--muted); padding: 3px 10px; border-radius: 5px; cursor: pointer; font-size: 11px; }
  .export-btn:hover { color: var(--text); border-color: var(--accent); }
  .table-card { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 20px; margin-bottom: 24px; overflow-x: auto; }
  .table-foot { display: flex; justify-content: flex-end; align-items: center; gap: 12px; margin-top: 12px; }
  .table-foot:empty { margin-top: 0; }
  .show-more-btn { background: transparent; border: 1px solid var(--border); color: var(--muted); padding: 4px 12px; border-radius: 6px; cursor: pointer; font-size: 12px; }
  .show-more-btn:hover { color: var(--text); border-color: var(--accent); }
  .show-more-link { color: var(--blue); text-decoration: none; font-size: 12px; cursor: pointer; }
  .show-more-link:hover { text-decoration: underline; }

  footer { border-top: 1px solid var(--border); padding: 20px 24px; margin-top: 8px; }
  .footer-content { max-width: 1400px; margin: 0 auto; }
  .footer-content p { color: var(--muted); font-size: 12px; line-height: 1.7; margin-bottom: 4px; }
  .footer-content p:last-child { margin-bottom: 0; }
  .footer-content a { color: var(--blue); text-decoration: none; }
  .footer-content a:hover { text-decoration: underline; }
  .footer-content a.update-link { color: var(--accent); font-weight: 600; }

  /* Jump bar — a sticky table-of-contents for a long report. Styled as a sibling
     of the filter bar (same card surface + bottom border) so it reads as part of
     the same control strip. It pins to the viewport top once the header/filter
     scroll away. z-index sits below the model panel (50) so the dropdown still
     overlays it. */
  /* Inline info affordance (e.g. the dispatches table) — native title tooltip. */
  .info-icon { display: inline-flex; align-items: center; vertical-align: middle; margin-left: 3px; color: var(--muted); cursor: help; }
  .info-icon svg { display: block; }
  .info-icon:hover { color: var(--text); }

  /* Collapsible cards — a full section fold, independent of in-table Show
     more/less (which only pages rows). Collapsing hides the card body and its
     header controls, leaving just the caret + title. State persists per card in
     localStorage. */
  .card-caret { display: inline-block; width: 0.9em; margin-right: 7px; font-size: 14px; line-height: 1; color: inherit; transform: rotate(90deg); transition: transform 0.15s; }
  .collapsed .card-caret { transform: rotate(0deg); }
  .chart-card > h2, .chart-header > h2, .section-title { cursor: pointer; user-select: none; }
  .chart-card > h2:hover, .chart-header > h2:hover, .section-title:hover { color: var(--text); }
  .info-icon:focus-visible, .chart-card > h2:focus-visible, .chart-header > h2:focus-visible, .section-title:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  .chart-card.collapsed > h2, .chart-card.collapsed > .chart-header { margin-bottom: 0; }
  .table-card.collapsed > .section-title, .table-card.collapsed > .section-header { margin-bottom: 0; }
  .chart-card.collapsed > *:not(h2):not(.chart-header),
  .chart-card.collapsed .chart-header > *:not(h2),
  .table-card.collapsed > *:not(.section-title):not(.section-header),
  .table-card.collapsed .section-header > *:not(.section-title) { display: none; }

  @media (max-width: 768px) { .charts-grid { grid-template-columns: 1fr; } .chart-card.wide { grid-column: 1; } }
</style>
</head>
<body>
<header>
  <div class="header-title">
    <span class="header-icon" role="img" aria-label="Claude Usage"></span>
    <h1>Claude Code Usage</h1>
  </div>
  <div class="meta" id="meta">Loading...</div>
  <button id="rescan-btn" onclick="triggerRescan()" title="Scan for new usage since the last update. Adds new turns without affecting existing history.">&#x21bb; Rescan</button>
</header>

<div class="container">
  <div class="stats-row" id="stats-row"></div>
  <div class="charts-grid">
    <div class="chart-card wide" id="sec-daily" data-card="daily">
      <h2><span class="card-caret">&#9656;</span><span id="daily-chart-title">Daily Token Usage — Last 30 Days</span></h2>
      <div class="chart-wrap tall"><canvas id="chart-daily"></canvas></div>
    </div>
    <div class="chart-card wide" id="sec-hourly" data-card="hourly">
      <div class="chart-header">
        <h2><span class="card-caret">&#9656;</span><span id="hourly-chart-title">Average Hourly Distribution — Last 30 Days</span></h2>
        <div class="chart-header-right">
          <span class="peak-legend" title="Mon–Fri 05:00–11:00 PT — Anthropic peak-hour throttling window"><span class="peak-swatch"></span>Peak hours (PT)</span>
          <span class="chart-day-count" id="hourly-day-count"></span>
          <div class="tz-group">
            <button class="tz-btn" data-tz="local" onclick="setHourlyTZ('local')">Local</button>
            <button class="tz-btn" data-tz="utc"   onclick="setHourlyTZ('utc')">UTC</button>
          </div>
        </div>
      </div>
      <div class="chart-wrap"><canvas id="chart-hourly"></canvas></div>
    </div>
  </div>

  <div id="filter-bar">
    <div class="filter-label">Models</div>
    <div class="model-select" id="model-select">
      <button class="model-trigger" id="model-trigger" aria-haspopup="true" aria-expanded="false" onclick="toggleModelPanel(event)">
        <span id="model-trigger-label">All models</span>
        <span class="model-caret">&#9662;</span>
      </button>
      <div class="model-panel" id="model-panel" hidden>
        <div class="model-panel-actions">
          <button class="filter-btn" onclick="selectAllModels()">All</button>
          <button class="filter-btn" onclick="clearAllModels()">None</button>
        </div>
        <div id="model-checkboxes"></div>
      </div>
    </div>
    <div class="filter-sep"></div>
    <div class="filter-label">Projects</div>
    <div class="model-select" id="project-select">
      <button class="model-trigger" id="project-trigger" aria-haspopup="true" aria-expanded="false" onclick="toggleProjectPanel(event)">
        <span id="project-trigger-label">All projects</span>
        <span class="model-caret">&#9662;</span>
      </button>
      <div class="model-panel" id="project-panel" hidden>
        <div class="model-panel-actions">
          <button class="filter-btn" onclick="selectAllProjects()">All</button>
          <button class="filter-btn" onclick="clearAllProjects()">None</button>
        </div>
        <div id="project-checkboxes"></div>
      </div>
    </div>
    <div class="filter-sep"></div>
    <div class="filter-label">Range</div>
    <div class="range-select">
      <select id="range-select" aria-label="Date range" onchange="setRange(this.value)">
        <option value="today">Today</option>
        <option value="yesterday">Yesterday</option>
        <option value="week">This Week</option>
        <option value="month">This Month</option>
        <option value="prev-month">Previous Month</option>
        <option value="7d">Last 7 Days</option>
        <option value="30d">Last 30 Days</option>
        <option value="90d">Last 90 Days</option>
        <option value="all">All Time</option>
      </select>
    </div>
    <div class="filter-hint">Filters below apply to everything from Usage Timeline down.</div>
  </div>

  <div class="charts-grid">
    <div class="chart-card wide" id="sec-timeline" data-card="timeline">
      <div class="chart-header">
        <h2><span class="card-caret">&#9656;</span><span id="timeline-chart-title">Usage Timeline by Project</span></h2>
        <div class="chart-header-right">
          <span class="chart-day-count" id="timeline-note"></span>
          <button class="tz-btn" id="timeline-reset-btn" onclick="resetTimelineSelection()" disabled>Reset selection</button>
          <div class="tz-group">
            <button class="tz-btn" data-gran="day"    onclick="setTimelineGranularity('day')">Day</button>
            <button class="tz-btn" data-gran="hour"   onclick="setTimelineGranularity('hour')">Hour</button>
            <button class="tz-btn" data-gran="minute" onclick="setTimelineGranularity('minute')">Minute</button>
          </div>
        </div>
      </div>
      <div class="timeline-body">
        <div class="chart-wrap tall timeline-chart-wrap"><canvas id="chart-timeline"></canvas></div>
        <div class="timeline-side">
          <div class="timeline-side-title" id="timeline-side-title">By Project — full range</div>
          <table class="timeline-side-table">
            <thead><tr><th>Project</th><th>Tokens</th></tr></thead>
            <tbody id="timeline-project-body"></tbody>
          </table>
        </div>
      </div>
      <p class="timeline-hint">Drag across the chart to select a time range — narrows every chart/table below. Double-click or "Reset selection" to clear.</p>
    </div>
    <div class="chart-card" id="sec-models" data-card="model-chart">
      <h2><span class="card-caret">&#9656;</span>By Model</h2>
      <div class="chart-wrap"><canvas id="chart-model"></canvas></div>
    </div>
    <div class="chart-card" id="sec-projects" data-card="project-chart">
      <h2><span class="card-caret">&#9656;</span>Top Projects by Tokens</h2>
      <div class="chart-wrap"><canvas id="chart-project"></canvas></div>
    </div>
    <div class="chart-card wide" id="sec-subagents" data-card="subagent-chart">
      <h2><span class="card-caret">&#9656;</span><span id="subagent-chart-title">Subagent Tokens by Type</span></h2>
      <div class="chart-wrap"><canvas id="chart-subagent"></canvas></div>
    </div>
  </div>
  <div class="table-card" id="sec-cost-model" data-card="cost-by-model">
    <div class="section-title"><span class="card-caret">&#9656;</span>Cost by Model</div>
    <table>
      <thead><tr>
        <th>Model</th>
        <th class="sortable" onclick="setModelSort('turns')">Turns <span class="sort-icon" id="msort-turns"></span></th>
        <th class="sortable" onclick="setModelSort('input')">Input <span class="sort-icon" id="msort-input"></span></th>
        <th class="sortable" onclick="setModelSort('output')">Output <span class="sort-icon" id="msort-output"></span></th>
        <th class="sortable" onclick="setModelSort('cache_read')">Cache Read <span class="sort-icon" id="msort-cache_read"></span></th>
        <th class="sortable" onclick="setModelSort('cache_creation')">Cache Creation <span class="sort-icon" id="msort-cache_creation"></span></th>
        <th class="sortable" onclick="setModelSort('cost')">Est. Cost <span class="sort-icon" id="msort-cost"></span></th>
      </tr></thead>
      <tbody id="model-cost-body"></tbody>
    </table>
    <div class="table-foot" id="model-cost-foot"></div>
  </div>
  <div class="table-card" id="sec-dispatches" data-card="dispatches">
    <div class="section-header"><div class="section-title"><span class="card-caret">&#9656;</span>Top Subagent Dispatches <span class="info-icon" tabindex="0" role="img" aria-label="About this table" title="Ranked by total tokens. &quot;unknown&quot; means the parent dispatch record wasn't found."><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/><path d="M12 8h.01"/></svg></span></div><button class="export-btn" onclick="exportDispatchesCSV()" title="Export all filtered subagent dispatches to CSV">&#x2913; CSV</button></div>
    <table>
      <thead><tr>
        <th>Type</th><th>Started</th><th>Model</th><th>Turns</th><th>Tool Uses</th>
        <th>Duration</th><th>Input</th><th>Output</th><th>Cache Read</th><th>Tokens</th><th>Est. Cost</th>
      </tr></thead>
      <tbody id="dispatches-body"></tbody>
    </table>
    <div class="table-foot" id="dispatches-foot"></div>
  </div>
  <div class="table-card" id="sec-skill-cost" data-card="cost-by-skill">
    <div class="section-header"><div class="section-title"><span class="card-caret">&#9656;</span>Token Usage by Skill</div><button class="export-btn" onclick="exportSkillsCSV()" title="Export all filtered skill usage to CSV">&#x2913; CSV</button></div>
    <table>
      <thead><tr>
        <th>Skill</th>
        <th class="sortable" onclick="setSkillSort('turns')">Turns <span class="sort-icon" id="sksort-turns"></span></th>
        <th class="sortable" onclick="setSkillSort('input')">Input <span class="sort-icon" id="sksort-input"></span></th>
        <th class="sortable" onclick="setSkillSort('output')">Output <span class="sort-icon" id="sksort-output"></span></th>
        <th class="sortable" onclick="setSkillSort('cache_read')">Cache Read <span class="sort-icon" id="sksort-cache_read"></span></th>
        <th class="sortable" onclick="setSkillSort('cache_creation')">Cache Creation <span class="sort-icon" id="sksort-cache_creation"></span></th>
        <th class="sortable" onclick="setSkillSort('cost')">Est. Cost <span class="sort-icon" id="sksort-cost"></span></th>
      </tr></thead>
      <tbody id="skill-cost-body"></tbody>
    </table>
    <div class="table-foot" id="skill-cost-foot"></div>
  </div>
  <div class="table-card" id="sec-tool-cost" data-card="cost-by-tool">
    <div class="section-header"><div class="section-title"><span class="card-caret">&#9656;</span>Token Usage by MCP / CLI</div><button class="export-btn" onclick="exportToolsCSV()" title="Export all filtered tool usage to CSV">&#x2913; CSV</button></div>
    <table>
      <thead><tr>
        <th>Tool</th>
        <th class="sortable" onclick="setToolSort('turns')">Turns <span class="sort-icon" id="tlsort-turns"></span></th>
        <th class="sortable" onclick="setToolSort('input')">Input <span class="sort-icon" id="tlsort-input"></span></th>
        <th class="sortable" onclick="setToolSort('output')">Output <span class="sort-icon" id="tlsort-output"></span></th>
        <th class="sortable" onclick="setToolSort('cache_read')">Cache Read <span class="sort-icon" id="tlsort-cache_read"></span></th>
        <th class="sortable" onclick="setToolSort('cache_creation')">Cache Creation <span class="sort-icon" id="tlsort-cache_creation"></span></th>
        <th class="sortable" onclick="setToolSort('cost')">Est. Cost <span class="sort-icon" id="tlsort-cost"></span></th>
      </tr></thead>
      <tbody id="tool-cost-body"></tbody>
    </table>
    <div class="table-foot" id="tool-cost-foot"></div>
  </div>
  <div class="table-card" id="sec-sessions" data-card="sessions">
    <div class="section-header"><div class="section-title"><span class="card-caret">&#9656;</span>Recent Sessions</div><button class="export-btn" onclick="exportSessionsCSV()" title="Export all filtered sessions to CSV">&#x2913; CSV</button></div>
    <table>
      <thead><tr>
        <th>Session</th>
        <th>Project</th>
        <th>Title</th>
        <th class="sortable" onclick="setSessionSort('last')">Last Active <span class="sort-icon" id="sort-icon-last"></span></th>
        <th class="sortable" onclick="setSessionSort('duration_min')">Duration <span class="sort-icon" id="sort-icon-duration_min"></span></th>
        <th>Model</th>
        <th class="sortable" onclick="setSessionSort('turns')">Turns <span class="sort-icon" id="sort-icon-turns"></span></th>
        <th class="sortable" onclick="setSessionSort('input')">Input <span class="sort-icon" id="sort-icon-input"></span></th>
        <th class="sortable" onclick="setSessionSort('output')">Output <span class="sort-icon" id="sort-icon-output"></span></th>
        <th class="sortable" onclick="setSessionSort('cost')">Est. Cost <span class="sort-icon" id="sort-icon-cost"></span></th>
      </tr></thead>
      <tbody id="sessions-body"></tbody>
    </table>
    <div class="table-foot" id="sessions-foot"></div>
  </div>
  <div class="table-card" id="sec-cost-project" data-card="cost-by-project">
    <div class="section-header"><div class="section-title"><span class="card-caret">&#9656;</span>Cost by Project</div><button class="export-btn" onclick="exportProjectsCSV()" title="Export all projects to CSV">&#x2913; CSV</button></div>
    <table>
      <thead><tr>
        <th>Project</th>
        <th class="sortable" onclick="setProjectSort('sessions')">Sessions <span class="sort-icon" id="psort-sessions"></span></th>
        <th class="sortable" onclick="setProjectSort('turns')">Turns <span class="sort-icon" id="psort-turns"></span></th>
        <th class="sortable" onclick="setProjectSort('input')">Input <span class="sort-icon" id="psort-input"></span></th>
        <th class="sortable" onclick="setProjectSort('output')">Output <span class="sort-icon" id="psort-output"></span></th>
        <th class="sortable" onclick="setProjectSort('cost')">Est. Cost <span class="sort-icon" id="psort-cost"></span></th>
      </tr></thead>
      <tbody id="project-cost-body"></tbody>
    </table>
    <div class="table-foot" id="project-cost-foot"></div>
  </div>
</div>

<footer>
  <div class="footer-content">
    <p>Cost estimates based on Anthropic API pricing (<a href="https://claude.com/pricing#api" target="_blank">claude.com/pricing#api</a>) as of June 2026. Only models containing <em>fable</em>, <em>mythos</em>, <em>opus</em>, <em>sonnet</em>, or <em>haiku</em> in the name are included in cost calculations. Actual costs for Max/Pro subscribers differ from API pricing.</p>
    <p>
      GitHub: <a href="https://github.com/phuryn/claude-usage" target="_blank">https://github.com/phuryn/claude-usage</a>
      &nbsp;&middot;&nbsp;
      Created by: <a href="https://www.productcompass.pm" target="_blank">The Product Compass Newsletter</a>
      &nbsp;&middot;&nbsp;
      License: MIT
    </p>
    <p id="footer-meta"></p>
  </div>
</footer>

<script>
// ── Helpers ────────────────────────────────────────────────────────────────
function esc(s) {
  const d = document.createElement('div');
  d.textContent = String(s);
  return d.innerHTML;
}

// ── State ──────────────────────────────────────────────────────────────────
let rawData = null;
let selectedModels = new Set();
let allModelsList = [];
let selectedRange = '30d';
let charts = {};
let sessionSortCol = 'last';
let modelSortCol = 'cost';
let modelSortDir = 'desc';
let projectSortCol = 'cost';
let projectSortDir = 'desc';
let skillSortCol = 'cost';
let skillSortDir = 'desc';
let toolSortCol = 'cost';
let toolSortDir = 'desc';
let lastFilteredSessions = [];
let lastByModel = [];
let lastByProject = [];
let lastBySkill = [];
let lastByTool = [];
let lastFilteredDispatches = [];
let sessionSortDir = 'desc';

// Tables reveal rows in steps: 10 -> 25 -> 50, capped at 50 because rendering
// more than that visibly hurts performance. Past 50 the footer offers a
// "Download CSV to see more" link instead of another in-table step, plus a
// Show less button that resets straight back to 10. Limits persist across
// re-renders so sorting/filtering keeps the user's chosen depth (visible rows
// always reflect the active sort).
const TABLE_STEPS = [10, 25, 50];
const TABLE_MAX = TABLE_STEPS[TABLE_STEPS.length - 1];  // hard cap on in-table rows
// Don't paginate a table that barely exceeds the first step — paging away one or
// two rows just to show a "Show more" button is more annoying than helpful. Below
// this many rows a table always renders in full (no toggle).
const PAGINATE_THRESHOLD = 12;
function nextTableLimit(current, total) {
  for (const s of TABLE_STEPS) {
    if (s > current && s < total) return s;
  }
  return Math.min(total, TABLE_MAX);  // reveal everything, but never past the cap
}
// Rows to actually show: everything when the table is small enough to skip
// paging, otherwise the user's current step.
function shownCount(limit, total) {
  return total <= PAGINATE_THRESHOLD ? total : limit;
}
let modelLimit = TABLE_STEPS[0];
let sessionsLimit = TABLE_STEPS[0];
let projectLimit = TABLE_STEPS[0];
let skillLimit = TABLE_STEPS[0];
let toolLimit = TABLE_STEPS[0];
let dispatchesLimit = TABLE_STEPS[0];
let hourlyTZ = 'local';  // 'local' or 'utc'

// ── Peak-hour config ───────────────────────────────────────────────────────
// Anthropic throttles Mon–Fri 05:00–11:00 PT. We approximate as fixed UTC hours
// 12–17 (matches PDT; during PST the window shifts by 1h — accepted simplification).
const PEAK_HOURS_UTC = new Set([12, 13, 14, 15, 16, 17]);

// Local-timezone offset in hours (signed). Fractional offsets (e.g. India UTC+5:30)
// are rounded to the nearest hour for bucket alignment.
function localOffsetHours() {
  return Math.round(-new Date().getTimezoneOffset() / 60);
}

// Return the UTC hour (0–23) corresponding to a displayed-hour bucket.
function displayHourToUTC(displayHour, tzMode) {
  if (tzMode === 'utc') return displayHour;
  return ((displayHour - localOffsetHours()) % 24 + 24) % 24;
}

// Return the displayed-hour bucket for a UTC hour.
function utcHourToDisplay(utcHour, tzMode) {
  if (tzMode === 'utc') return utcHour;
  return ((utcHour + localOffsetHours()) % 24 + 24) % 24;
}

function isPeakHour(displayHour, tzMode) {
  return PEAK_HOURS_UTC.has(displayHourToUTC(displayHour, tzMode));
}

function formatHourLabel(h) {
  return String(h).padStart(2, '0') + ':00';
}

function tzDisplayName(tzMode) {
  if (tzMode === 'utc') return 'UTC';
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || 'Local';
  } catch(e) {
    return 'Local';
  }
}

// ── Pricing (Anthropic API, June 2026) ─────────────────────────────────────
const PRICING = {
  // Fable / Mythos — Anthropic's most capable class, priced at 2x Opus.
  // (Mythos 5 shares Fable 5's pricing; Project-Glasswing access only.)
  'claude-fable-5':    { input: 10.00, output: 50.00, cache_write: 12.50, cache_read: 1.00 },
  'claude-mythos-5':   { input: 10.00, output: 50.00, cache_write: 12.50, cache_read: 1.00 },
  'claude-opus-4-8':   { input:  5.00, output: 25.00, cache_write:  6.25, cache_read: 0.50 },
  'claude-opus-4-7':   { input:  5.00, output: 25.00, cache_write:  6.25, cache_read: 0.50 },
  'claude-opus-4-6':   { input:  5.00, output: 25.00, cache_write:  6.25, cache_read: 0.50 },
  'claude-opus-4-5':   { input:  5.00, output: 25.00, cache_write:  6.25, cache_read: 0.50 },
  'claude-sonnet-4-7': { input:  3.00, output: 15.00, cache_write:  3.75, cache_read: 0.30 },
  'claude-sonnet-4-6': { input:  3.00, output: 15.00, cache_write:  3.75, cache_read: 0.30 },
  'claude-sonnet-4-5': { input:  3.00, output: 15.00, cache_write:  3.75, cache_read: 0.30 },
  'claude-haiku-4-7':  { input:  1.00, output:  5.00, cache_write:  1.25, cache_read: 0.10 },
  'claude-haiku-4-6':  { input:  1.00, output:  5.00, cache_write:  1.25, cache_read: 0.10 },
  'claude-haiku-4-5':  { input:  1.00, output:  5.00, cache_write:  1.25, cache_read: 0.10 },
};

function isBillable(model) {
  if (!model) return false;
  const m = model.toLowerCase();
  return m.includes('fable') || m.includes('mythos') ||
         m.includes('opus') || m.includes('sonnet') || m.includes('haiku');
}

function getPricing(model) {
  if (!model) return null;
  if (PRICING[model]) return PRICING[model];
  for (const key of Object.keys(PRICING)) {
    if (model.startsWith(key)) return PRICING[key];
  }
  const m = model.toLowerCase();
  if (m.includes('fable') || m.includes('mythos')) return PRICING['claude-fable-5'];
  if (m.includes('opus'))   return PRICING['claude-opus-4-8'];
  if (m.includes('sonnet')) return PRICING['claude-sonnet-4-6'];
  if (m.includes('haiku'))  return PRICING['claude-haiku-4-5'];
  return null;
}

function calcCost(model, inp, out, cacheRead, cacheCreation) {
  if (!isBillable(model)) return 0;
  const p = getPricing(model);
  if (!p) return 0;
  return (
    inp           * p.input       / 1e6 +
    out           * p.output      / 1e6 +
    cacheRead     * p.cache_read  / 1e6 +
    cacheCreation * p.cache_write / 1e6
  );
}

// ── Formatting ─────────────────────────────────────────────────────────────
function fmt(n) {
  if (n >= 1e9) return (n/1e9).toFixed(2)+'B';
  if (n >= 1e6) return (n/1e6).toFixed(2)+'M';
  if (n >= 1e3) return (n/1e3).toFixed(1)+'K';
  return n.toLocaleString();
}
function fmtCost(c)    { return '$' + c.toLocaleString(undefined, { minimumFractionDigits: 4, maximumFractionDigits: 4 }); }
function fmtCostBig(c) { return '$' + c.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }); }

// ── Chart colors ───────────────────────────────────────────────────────────
// Warm/neutral palette kept in sync with the CSS :root variables so charts match
// the Claude Code interface (less blue). Chart legends/axes use C.axis (a touch
// lighter than --muted so small labels stay legible on the dark card); grid uses
// C.border.
const C = {
  text:   '#BFBFBF',
  muted:  '#4F4F50',
  axis:   '#6F6F70',
  border: '#2C2D2E',
  card:   '#1E1F20',
  blue:   '#48A0C7',
  green:  '#74C991',
  red:    '#C74E39',
  accent: '#d97757',
  amber:  '#D9A84E',
  purple: '#9B7EC7',
  teal:   '#5BB8A3',
  mauve:  '#C77E9B',
};
const TOKEN_COLORS = {
  input:          'rgba(72,160,199,0.85)',   // blue
  output:         'rgba(217,119,87,0.85)',    // accent / coral
  cache_read:     'rgba(116,201,145,0.75)',   // green
  cache_creation: 'rgba(217,168,78,0.75)',    // amber
};
// Hover lifts on a dark theme: bars/series go to full opacity (a touch brighter).
const TOKEN_HOVER = {
  input:          'rgba(72,160,199,1)',
  output:         'rgba(217,119,87,1)',
  cache_read:     'rgba(116,201,145,1)',
  cache_creation: 'rgba(217,168,78,1)',
};
// Donut / categorical palette — warm, Anthropic-leaning (clay, tan, sage, dusty
// blue, mauve, ochre, taupe, terracotta) rather than a saturated rainbow.
const MODEL_COLORS = ['#D97757','#C9A26B','#7FA98C','#6E97A8','#B98AA0','#D9A84E','#A88B6A','#C2705A'];

// Subagent type swatches (table tag tint) — warm/neutral, matching the palette.
const AGENT_TYPE_COLORS = {
  'general-purpose':   '#6E97A8',
  'Explore':           '#9B7EC7',
  'Plan':              '#D9A84E',
  'claude-code-guide': '#48A0C7',
  'auto-compact':      '#A88B6A',
  'unknown':           '#4F4F50',
};
function colorForAgentType(t) { return AGENT_TYPE_COLORS[t] || '#7FA98C'; }
function fmtDuration(ms) {
  if (!ms || ms < 0) return '—';
  const s = Math.round(ms / 1000);
  if (s < 60) return s + 's';
  const m = Math.floor(s / 60), r = s % 60;
  if (m < 60) return r ? `${m}m${r}s` : `${m}m`;
  const h = Math.floor(m / 60);
  return `${h}h${m % 60}m`;
}

// Tooltip color swatches: solid fill, no border (Chart.js's default draws a
// bordered box that looked offset/inconsistent). Lines use their solid stroke
// color instead of the translucent area fill.
Chart.defaults.color = C.axis;
// multiKeyBackground defaults to white and is drawn behind each tooltip swatch,
// peeking out as a thin white border on plain-box charts — make it transparent.
Chart.defaults.plugins.tooltip.multiKeyBackground = 'transparent';
Chart.defaults.plugins.tooltip.callbacks.labelColor = (ctx) => {
  const ds = ctx.dataset || {};
  let col = Array.isArray(ds.backgroundColor) ? ds.backgroundColor[ctx.dataIndex] : ds.backgroundColor;
  if (ds.type === 'line') col = ds.borderColor;
  return { borderColor: col, backgroundColor: col, borderWidth: 0 };
};

// Legend visibility must survive repaints (filter changes, auto-refresh, sort) —
// the charts are destroyed and rebuilt each render, which otherwise resets any
// series the user toggled off. We track hidden series by label per chart and
// reapply on rebuild: dataset charts via `dataset.hidden`, the doughnut via
// per-slice data visibility (see applyModelHidden).
const hiddenSeries = { daily: new Set(), hourly: new Set(), project: new Set(), model: new Set(), subagent: new Set(), timeline: new Set() };
function legendToggle(key) {
  return (e, item, legend) => {
    const ci = legend.chart;
    const ds = ci.data.datasets[item.datasetIndex];
    ds.hidden = !ds.hidden;
    if (ds.hidden) hiddenSeries[key].add(ds.label); else hiddenSeries[key].delete(ds.label);
    ci.update();
  };
}

// ── Time range ─────────────────────────────────────────────────────────────
const RANGE_LABELS = { 'today': 'Today', 'yesterday': 'Yesterday', 'week': 'This Week', 'month': 'This Month', 'prev-month': 'Previous Month', '7d': 'Last 7 Days', '30d': 'Last 30 Days', '90d': 'Last 90 Days', 'all': 'All Time' };
const RANGE_TICKS  = { 'today': 1, 'yesterday': 1, 'week': 7, 'month': 15, 'prev-month': 15, '7d': 7, '30d': 15, '90d': 13, 'all': 12 };
const VALID_RANGES = Object.keys(RANGE_LABELS);

function rangeIncludesToday(range) {
  if (range === 'all') return true;
  const { start, end } = getRangeBounds(range);
  const today = new Date().toISOString().slice(0, 10);
  if (start && today < start) return false;
  if (end && today > end) return false;
  return true;
}

function getRangeBounds(range) {
  if (range === 'all') return { start: null, end: null };
  const today = new Date();
  const iso = d => d.toISOString().slice(0, 10);
  if (range === 'today') {
    const t = iso(today);
    return { start: t, end: t };
  }
  if (range === 'yesterday') {
    const y = new Date(today); y.setDate(today.getDate() - 1);
    const t = iso(y);
    return { start: t, end: t };
  }
  if (range === 'week') {
    const day = today.getDay();
    const diffToMon = day === 0 ? 6 : day - 1;
    const mon = new Date(today); mon.setDate(today.getDate() - diffToMon);
    const sun = new Date(mon); sun.setDate(mon.getDate() + 6);
    return { start: iso(mon), end: iso(sun) };
  }
  if (range === 'month') {
    const start = new Date(today.getFullYear(), today.getMonth(), 1);
    const end = new Date(today.getFullYear(), today.getMonth() + 1, 0);
    return { start: iso(start), end: iso(end) };
  }
  if (range === 'prev-month') {
    const start = new Date(today.getFullYear(), today.getMonth() - 1, 1);
    const end = new Date(today.getFullYear(), today.getMonth(), 0);
    return { start: iso(start), end: iso(end) };
  }
  const days = range === '7d' ? 7 : range === '30d' ? 30 : 90;
  const d = new Date();
  d.setDate(d.getDate() - days);
  return { start: iso(d), end: null };
}

function readURLRange() {
  const p = new URLSearchParams(window.location.search).get('range');
  return VALID_RANGES.includes(p) ? p : '30d';
}

function setRange(range) {
  selectedRange = range;
  const sel = document.getElementById('range-select');
  if (sel) sel.value = range;  // keep the dropdown in sync with programmatic calls
  // A day-range change invalidates any hour/minute assumptions (minute is
  // only ever valid for Today/Yesterday) and any active drag-selection (its
  // bucket shape belongs to the old range) — reset both to this range's default.
  timelineGranularity = defaultTimelineGranularity();
  timelineSelection = null;
  timelineMinuteWindow = null;
  updateURL();
  applyFilter();
  scheduleAutoRefresh();
  fetchTimeline();  // range changed -> Usage Timeline is server-scoped, needs a fresh query
}

// -- Usage Timeline (day/hour/minute buckets grouped by project) -----------
// Queried fresh per range/granularity change rather than folded into rawData
// like the daily/hourly charts, since minute buckets over unbounded history
// would be a huge payload (see get_timeline_data's docstring server-side).
let timelineGranularity = 'day';
let timelineRawRows = [];
// A saved Hour-granularity selection, converted to minute-bucket bounds, so
// switching into Minute view zooms into that hour range instead of showing
// the whole day's 1440 buckets. Cleared whenever it's no longer applicable.
let timelineMinuteWindow = null;

function defaultTimelineGranularity() {
  return (selectedRange === 'today' || selectedRange === 'yesterday') ? 'hour' : 'day';
}

// Minute view is only offered once the user has drag-selected a genuine
// subset of the currently-visible Hour buckets -- otherwise there's nothing
// meaningful to zoom into at minute resolution.
function timelineMinuteAllowed() {
  return timelineGranularity === 'hour' && !!timelineSelection &&
    timelineBuckets.length > 0 &&
    (timelineSelection.start !== timelineBuckets[0] || timelineSelection.end !== timelineBuckets[timelineBuckets.length - 1]);
}

// Advance a plain 'YYYY-MM-DD' string by `delta` days, anchored in UTC so the
// calendar walk never shifts a day under a local-timezone DST transition.
function addDaysISO(dateStr, delta) {
  const [y, m, d] = dateStr.split('-').map(Number);
  const dt = new Date(Date.UTC(y, m - 1, d));
  dt.setUTCDate(dt.getUTCDate() + delta);
  return dt.toISOString().slice(0, 10);
}

// Advance a 'YYYY-MM-DD HH:MM' bucket string by one minute, UTC-anchored for
// the same DST-safety reason as addDaysISO.
function addMinuteISO(bucket) {
  const [datePart, timePart] = bucket.split(' ');
  const [y, m, d] = datePart.split('-').map(Number);
  const [hh, mm] = timePart.split(':').map(Number);
  const dt = new Date(Date.UTC(y, m - 1, d, hh, mm));
  dt.setUTCMinutes(dt.getUTCMinutes() + 1);
  const pad = n => String(n).padStart(2, '0');
  return dt.getUTCFullYear() + '-' + pad(dt.getUTCMonth() + 1) + '-' + pad(dt.getUTCDate()) +
    ' ' + pad(dt.getUTCHours()) + ':' + pad(dt.getUTCMinutes());
}

// Every day/hour/minute bucket the chart should show, including ones with no
// data (so the axis reads as a continuous timeline, not just the moments
// something happened). Returns null for an unbounded range (All Time, or the
// open-ended Nd ranges with no end) -- there's no fixed extent to fill, so
// the caller falls back to whatever buckets the data actually has.
function timelineExpectedBuckets(start, end, granularity, minuteWindow) {
  if (granularity === 'minute' && minuteWindow) {
    // Scoped to a specific hour range (from a prior Hour-granularity
    // selection) rather than the whole day, so switching to Minute actually
    // zooms in instead of dumping all 1440 buckets.
    const buckets = [];
    let cur = minuteWindow.start;
    let guard = 0;
    while (cur <= minuteWindow.end && guard < 1440) {
      buckets.push(cur);
      cur = addMinuteISO(cur);
      guard++;
    }
    return buckets;
  }

  if (!start && !end) return null;
  const startDate = start || end;
  const endDate = end || new Date().toISOString().slice(0, 10);
  const buckets = [];
  let d = startDate;
  for (let i = 0; i < 366 && d <= endDate; i++) {  // bounded walk, defensive against a bad range
    if (granularity === 'day') {
      buckets.push(d);
    } else {
      for (let h = 0; h < 24; h++) {
        if (granularity === 'hour') {
          buckets.push(d + ' ' + String(h).padStart(2, '0') + ':00');
        } else {
          for (let mi = 0; mi < 60; mi++) buckets.push(d + ' ' + String(h).padStart(2, '0') + ':' + String(mi).padStart(2, '0'));
        }
      }
    }
    d = addDaysISO(d, 1);
  }
  return buckets;
}

function setTimelineGranularity(g) {
  if (g === 'minute') {
    if (!timelineMinuteAllowed()) return;
    // Carry the current Hour selection over as the Minute view's window
    // (":00"->start of hour, ":59" -> end of that hour) instead of clearing it.
    timelineMinuteWindow = { start: timelineSelection.start, end: timelineSelection.end.slice(0, 13) + ':59' };
    timelineSelection = { start: timelineMinuteWindow.start, end: timelineMinuteWindow.end };
  } else {
    timelineMinuteWindow = null;
    timelineSelection = null;  // a Day/Hour selection's bucket shape doesn't carry over
  }
  timelineGranularity = g;
  fetchTimeline();
}

function updateTimelineButtons() {
  const minuteOk = timelineMinuteAllowed();
  document.querySelectorAll('#sec-timeline .tz-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.gran === timelineGranularity);
    if (btn.dataset.gran === 'minute') btn.disabled = !minuteOk;
  });
}

async function fetchTimeline() {
  updateTimelineButtons();

  document.getElementById('timeline-chart-title').textContent =
    'Usage Timeline by Project — ' + RANGE_LABELS[selectedRange];

  const { start, end } = getRangeBounds(selectedRange);
  const params = new URLSearchParams({ granularity: timelineGranularity });
  if (start) params.set('start', start);
  if (end) params.set('end', end);

  const note = document.getElementById('timeline-note');
  try {
    const resp = await fetch('/api/timeline?' + params.toString());
    const d = await resp.json();
    if (d.error) {
      if (note) note.textContent = d.error;
      timelineRawRows = [];
    } else {
      if (note) note.textContent = '';
      timelineRawRows = d.rows || [];
    }
  } catch (e) {
    console.error(e);
    timelineRawRows = [];
  }
  refilterTimeline();
}

// Client-side model+project re-filter of the already-fetched timeline rows --
// cheap, so model/project checkbox toggles (which call applyFilter, not
// fetchTimeline) can refresh this chart too without hitting the server again.
function refilterTimeline() {
  renderTimelineChart(timelineRawRows.filter(r => selectedModels.has(r.model) && selectedProjects.has(r.project)));
}

// Selection state for the drag-to-select range on the timeline chart. The
// chart itself is destroyed/rebuilt on every range/granularity/model change
// (same pattern as the rest of the dashboard), so an active drag-selection is
// intentionally cleared whenever that happens -- persisting it across a data
// change isn't worth the extra bookkeeping (except the Hour->Minute handoff
// above, which is the one case worth preserving).
let timelineBuckets = [];
let timelineFilteredRows = [];

function renderTimelineChart(rows) {
  timelineFilteredRows = rows;

  const projTotals = {};
  for (const r of rows) {
    const tok = r.input + r.output + r.cache_read + r.cache_creation;
    projTotals[r.project] = (projTotals[r.project] || 0) + tok;
  }
  // Cap series count so the stacked chart / legend stay readable -- fold the
  // long tail of smaller projects into "Other" rather than showing dozens.
  const topProjects = Object.entries(projTotals)
    .sort((a, b) => b[1] - a[1])
    .slice(0, 8)
    .map(([p]) => p);
  const topSet = new Set(topProjects);
  const hasOther = Object.keys(projTotals).length > topProjects.length;
  const labels = hasOther ? [...topProjects, 'Other'] : topProjects;

  const { start, end } = getRangeBounds(selectedRange);
  const buckets = timelineExpectedBuckets(start, end, timelineGranularity, timelineMinuteWindow)
    || [...new Set(rows.map(r => r.bucket))].sort();
  timelineBuckets = buckets;
  const bucketIndex = new Map(buckets.map((b, i) => [b, i]));
  const seriesMap = {};
  for (const label of labels) seriesMap[label] = buckets.map(() => 0);
  for (const r of rows) {
    if (!bucketIndex.has(r.bucket)) continue;  // outside the current minute window, if any
    const key = topSet.has(r.project) ? r.project : (hasOther ? 'Other' : null);
    if (!key) continue;
    seriesMap[key][bucketIndex.get(r.bucket)] += r.input + r.output + r.cache_read + r.cache_creation;
  }

  const ctx = document.getElementById('chart-timeline').getContext('2d');
  if (charts.timeline) charts.timeline.destroy();
  if (!buckets.length) { charts.timeline = null; renderTimelineProjectTable(rows, null); return; }

  charts.timeline = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: buckets,
      datasets: labels.map((label, i) => ({
        label,
        hidden: hiddenSeries.timeline.has(label),
        data: seriesMap[label],
        backgroundColor: MODEL_COLORS[i % MODEL_COLORS.length],
        hoverBackgroundColor: MODEL_COLORS[i % MODEL_COLORS.length],
        stack: 'timeline',
      })),
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { onClick: legendToggle('timeline'), labels: { color: C.axis, boxWidth: 12 } },
        // Drag across the bars to zoom into a range -- chartjs-plugin-zoom
        // (loaded via CDN alongside Chart.js) auto-registers against the
        // global Chart object, no explicit Chart.register() needed. Pan is
        // off so drag always means "select a range", not "scroll the chart".
        zoom: {
          zoom: {
            drag: { enabled: true, backgroundColor: 'rgba(217,119,87,0.25)', borderColor: 'rgba(217,119,87,0.6)', borderWidth: 1 },
            mode: 'x',
            onZoomComplete: ({ chart }) => applyTimelineSelectionFromChart(chart),
          },
          pan: { enabled: false },
          limits: { x: { min: 0, max: Math.max(buckets.length - 1, 0), minRange: 1 } },
        },
      },
      scales: {
        x: { stacked: true, ticks: { color: C.axis, maxTicksLimit: 15 }, grid: { color: C.border } },
        y: { stacked: true, ticks: { color: C.axis }, grid: { color: C.border } },
      },
    },
  });

  // Double-click resets the selection. chartjs-plugin-zoom's drag mode
  // doesn't add its own reset gesture, so this is wired directly on the
  // canvas rather than through a chart option.
  ctx.canvas.ondblclick = resetTimelineSelection;

  renderTimelineProjectTable(rows, timelineSelection);
}

// Reads the zoomed x-axis range (bar-index based, since our x scale is a
// category/labels axis) off the chart, persists it as the shared
// timelineSelection (which cascades down to every chart/table below via
// applyFilter), and re-aggregates the project subtotal table to match.
function applyTimelineSelectionFromChart(chart) {
  const xScale = chart.scales.x;
  if (!xScale || !timelineBuckets.length) return;
  const minIdx = Math.max(0, Math.round(xScale.min));
  const maxIdx = Math.min(timelineBuckets.length - 1, Math.round(xScale.max));
  if (minIdx <= 0 && maxIdx >= timelineBuckets.length - 1) {
    timelineSelection = null;
  } else {
    timelineSelection = { start: timelineBuckets[minIdx], end: timelineBuckets[maxIdx] };
  }
  updateTimelineButtons();  // Minute availability may have just changed
  const selectedBuckets = timelineSelection ? new Set(timelineBuckets.slice(minIdx, maxIdx + 1)) : null;
  const subset = selectedBuckets ? timelineFilteredRows.filter(r => selectedBuckets.has(r.bucket)) : timelineFilteredRows;
  renderTimelineProjectTable(subset, timelineSelection);
  applyFilter();  // cascade the selection to everything below the Timeline chart
}

function resetTimelineSelection() {
  if (charts.timeline && charts.timeline.resetZoom) charts.timeline.resetZoom();
  timelineSelection = null;
  timelineMinuteWindow = null;
  // Minute view has no meaning without a selection to scope it — drop back
  // to Hour instead of leaving the chart showing a full unscoped day.
  const needsRefetch = timelineGranularity === 'minute';
  if (needsRefetch) timelineGranularity = 'hour';
  updateTimelineButtons();
  if (needsRefetch) {
    fetchTimeline();  // hour has a different bucket set than minute — needs a fresh query
  } else {
    renderTimelineProjectTable(timelineFilteredRows, null);
  }
  applyFilter();
}

function renderTimelineProjectTable(rows, range) {
  const projMap = {};
  for (const r of rows) {
    const tok = r.input + r.output + r.cache_read + r.cache_creation;
    projMap[r.project] = (projMap[r.project] || 0) + tok;
  }
  const sorted = Object.entries(projMap).sort((a, b) => b[1] - a[1]);

  const titleEl = document.getElementById('timeline-side-title');
  if (titleEl) titleEl.textContent = range ? 'By Project — ' + range.start + ' to ' + range.end : 'By Project — full range';

  const resetBtn = document.getElementById('timeline-reset-btn');
  if (resetBtn) resetBtn.disabled = !range;

  const body = document.getElementById('timeline-project-body');
  if (!body) return;
  body.innerHTML = sorted.length
    ? sorted.map(([project, tokens]) => '<tr><td title="' + esc(project) + '">' + esc(project) + '</td><td>' + tokens.toLocaleString() + '</td></tr>').join('')
    : '<tr><td colspan="2">No data</td></tr>';
}

function setHourlyTZ(mode) {
  hourlyTZ = mode;
  document.querySelectorAll('.tz-btn').forEach(btn =>
    btn.classList.toggle('active', btn.dataset.tz === mode)
  );
  applyFilter();
}

// ── Model filter ───────────────────────────────────────────────────────────
function modelPriority(m) {
  const ml = m.toLowerCase();
  if (ml.includes('fable') || ml.includes('mythos')) return 0;
  if (ml.includes('opus'))   return 1;
  if (ml.includes('sonnet')) return 2;
  if (ml.includes('haiku'))  return 3;
  return 4;
}

function sortedModels(models) {
  return [...models].sort((a, b) => {
    const pa = modelPriority(a), pb = modelPriority(b);
    return pa !== pb ? pa - pb : a.localeCompare(b);
  });
}

// Compact display name for the collapsed trigger, e.g. "claude-opus-4-8" ->
// "Opus 4.8", "claude-fable-5" -> "Fable 5". Non-Anthropic ids fall back to the
// basename with any provider prefix and trailing date suffix stripped.
function shortModelName(m) {
  const ml = m.toLowerCase();
  let family = null;
  if (ml.includes('fable'))       family = 'Fable';
  else if (ml.includes('mythos')) family = 'Mythos';
  else if (ml.includes('opus'))   family = 'Opus';
  else if (ml.includes('sonnet')) family = 'Sonnet';
  else if (ml.includes('haiku'))  family = 'Haiku';
  if (family) {
    const two = m.match(/(\d+)[._-](\d+)/);
    if (two) return family + ' ' + two[1] + '.' + two[2];
    const one = m.match(/(\d+)/);
    return one ? family + ' ' + one[1] : family;
  }
  let base = m.split('/').pop().split(':')[0];
  base = base.replace(/[-_]?\d{6,}.*$/, '');
  return base || m;
}

function readURLModels(allModels) {
  const param = new URLSearchParams(window.location.search).get('models');
  if (!param) {
    const billable = allModels.filter(m => isBillable(m));
    // Fallback: if the user only has non-billable / unknown models (e.g. all
    // local-LLM runs), default to all models so the dashboard isn't blank.
    return new Set(billable.length ? billable : allModels);
  }
  const fromURL = new Set(param.split(',').map(s => s.trim()).filter(Boolean));
  return new Set(allModels.filter(m => fromURL.has(m)));
}

function isDefaultModelSelection(allModels) {
  const billable = allModels.filter(m => isBillable(m));
  const expected = billable.length ? billable : allModels;
  if (selectedModels.size !== expected.length) return false;
  return expected.every(m => selectedModels.has(m));
}

function buildFilterUI(allModels) {
  allModelsList = [...allModels];
  selectedModels = readURLModels(allModels);
  const sorted = sortedModels(allModels);
  const anthropic = sorted.filter(m => isBillable(m));
  const other     = sorted.filter(m => !isBillable(m));
  const rowHTML = m => {
    const checked = selectedModels.has(m);
    return `<label class="model-cb-label ${checked ? 'checked' : ''}" data-model="${esc(m)}" title="${esc(m)}">
      <input type="checkbox" value="${esc(m)}" ${checked ? 'checked' : ''} onchange="onModelToggle(this)">
      <span class="model-cb-box">&#10003;</span>
      <span class="model-cb-text">${esc(m)}</span>
    </label>`;
  };
  let html = '';
  // Only show a group heading when both groups are present — a single-group
  // list doesn't need a label.
  const labelled = anthropic.length && other.length;
  if (anthropic.length) {
    if (labelled) html += '<div class="model-group-label">Anthropic</div>';
    html += anthropic.map(rowHTML).join('');
  }
  if (other.length) {
    if (labelled) html += '<div class="model-group-label">Other providers</div>';
    html += other.map(rowHTML).join('');
  }
  document.getElementById('model-checkboxes').innerHTML = html;
  updateModelTriggerLabel();
}

// Collapsed trigger text, in priority order:
//   "All models"     — everything selected
//   "No models"      — nothing selected
//   "All Anthropic"  — every Anthropic model (opus/sonnet/haiku/mythos/fable)
//                      selected and no other provider; "+N" if some others too
//   "Fable 5, Opus 4.7 +5" — otherwise, first two names + overflow count
function updateModelTriggerLabel() {
  const labelEl = document.getElementById('model-trigger-label');
  if (!labelEl) return;
  const n = selectedModels.size;
  if (n === 0)                    { labelEl.textContent = 'No models';  return; }
  if (n === allModelsList.length) { labelEl.textContent = 'All models'; return; }
  const anthropic = allModelsList.filter(m => isBillable(m));
  const others    = allModelsList.filter(m => !isBillable(m));
  if (anthropic.length && anthropic.every(m => selectedModels.has(m))) {
    // n < total (handled above), so when others exist at least one is unselected.
    const otherSel = others.filter(m => selectedModels.has(m)).length;
    labelEl.textContent = otherSel ? 'All Anthropic +' + otherSel : 'All Anthropic';
    return;
  }
  const chosen = sortedModels(allModelsList).filter(m => selectedModels.has(m));
  const shown = chosen.slice(0, 2).map(shortModelName);
  const extra = chosen.length - shown.length;
  labelEl.textContent = shown.join(', ') + (extra > 0 ? ' +' + extra : '');
}

function toggleModelPanel(event) {
  if (event) event.stopPropagation();
  const panel = document.getElementById('model-panel');
  const trigger = document.getElementById('model-trigger');
  const open = panel.hidden;
  panel.hidden = !open;
  trigger.classList.toggle('open', open);
  trigger.setAttribute('aria-expanded', open ? 'true' : 'false');
}

function closeModelPanel() {
  const panel = document.getElementById('model-panel');
  if (!panel || panel.hidden) return;
  panel.hidden = true;
  const trigger = document.getElementById('model-trigger');
  trigger.classList.remove('open');
  trigger.setAttribute('aria-expanded', 'false');
}

// Close the panel on outside click or Escape. Clicks inside #model-select
// (including the checkboxes and All/None) keep it open so multiple models can
// be toggled in one pass.
document.addEventListener('click', (e) => {
  const sel = document.getElementById('model-select');
  if (sel && !sel.contains(e.target)) closeModelPanel();
  const psel = document.getElementById('project-select');
  if (psel && !psel.contains(e.target)) closeProjectPanel();
});
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') { closeModelPanel(); closeProjectPanel(); } });

function onModelToggle(cb) {
  const label = cb.closest('label');
  if (cb.checked) { selectedModels.add(cb.value);    label.classList.add('checked'); }
  else            { selectedModels.delete(cb.value); label.classList.remove('checked'); }
  updateModelTriggerLabel();
  updateURL();
  applyFilter();
}

function selectAllModels() {
  document.querySelectorAll('#model-checkboxes input').forEach(cb => {
    cb.checked = true; selectedModels.add(cb.value); cb.closest('label').classList.add('checked');
  });
  updateModelTriggerLabel(); updateURL(); applyFilter();
}

function clearAllModels() {
  document.querySelectorAll('#model-checkboxes input').forEach(cb => {
    cb.checked = false; selectedModels.delete(cb.value); cb.closest('label').classList.remove('checked');
  });
  updateModelTriggerLabel(); updateURL(); applyFilter();
}

// ── Project filter (mirrors the Model filter above, minus the Anthropic/other
// grouping — projects have no natural grouping) ─────────────────────────────
let selectedProjects = new Set();
let allProjectsList = [];

function readURLProjects(allProjects) {
  const param = new URLSearchParams(window.location.search).get('projects');
  if (!param) return new Set(allProjects);
  const fromURL = new Set(param.split(',').map(s => s.trim()).filter(Boolean));
  return new Set(allProjects.filter(p => fromURL.has(p)));
}

function isDefaultProjectSelection(allProjects) {
  if (selectedProjects.size !== allProjects.length) return false;
  return allProjects.every(p => selectedProjects.has(p));
}

function buildProjectFilterUI(allProjects) {
  allProjectsList = [...allProjects];
  selectedProjects = readURLProjects(allProjects);
  const sorted = [...allProjects].sort((a, b) => a.localeCompare(b));
  const rowHTML = p => {
    const checked = selectedProjects.has(p);
    return `<label class="model-cb-label ${checked ? 'checked' : ''}" data-model="${esc(p)}" title="${esc(p)}">
      <input type="checkbox" value="${esc(p)}" ${checked ? 'checked' : ''} onchange="onProjectToggle(this)">
      <span class="model-cb-box">&#10003;</span>
      <span class="model-cb-text">${esc(p)}</span>
    </label>`;
  };
  document.getElementById('project-checkboxes').innerHTML = sorted.map(rowHTML).join('');
  updateProjectTriggerLabel();
}

function updateProjectTriggerLabel() {
  const labelEl = document.getElementById('project-trigger-label');
  if (!labelEl) return;
  const n = selectedProjects.size;
  if (n === 0)                      { labelEl.textContent = 'No projects';  return; }
  if (n === allProjectsList.length) { labelEl.textContent = 'All projects'; return; }
  const chosen = [...allProjectsList].filter(p => selectedProjects.has(p)).sort((a, b) => a.localeCompare(b));
  const shown = chosen.slice(0, 2);
  const extra = chosen.length - shown.length;
  labelEl.textContent = shown.join(', ') + (extra > 0 ? ' +' + extra : '');
}

function toggleProjectPanel(event) {
  if (event) event.stopPropagation();
  const panel = document.getElementById('project-panel');
  const trigger = document.getElementById('project-trigger');
  const open = panel.hidden;
  panel.hidden = !open;
  trigger.classList.toggle('open', open);
  trigger.setAttribute('aria-expanded', open ? 'true' : 'false');
}

function closeProjectPanel() {
  const panel = document.getElementById('project-panel');
  if (!panel || panel.hidden) return;
  panel.hidden = true;
  const trigger = document.getElementById('project-trigger');
  trigger.classList.remove('open');
  trigger.setAttribute('aria-expanded', 'false');
}

function onProjectToggle(cb) {
  const label = cb.closest('label');
  if (cb.checked) { selectedProjects.add(cb.value);    label.classList.add('checked'); }
  else            { selectedProjects.delete(cb.value); label.classList.remove('checked'); }
  updateProjectTriggerLabel();
  updateURL();
  applyFilter();
  fetchTimeline();
}

function selectAllProjects() {
  document.querySelectorAll('#project-checkboxes input').forEach(cb => {
    cb.checked = true; selectedProjects.add(cb.value); cb.closest('label').classList.add('checked');
  });
  updateProjectTriggerLabel(); updateURL(); applyFilter(); fetchTimeline();
}

function clearAllProjects() {
  document.querySelectorAll('#project-checkboxes input').forEach(cb => {
    cb.checked = false; selectedProjects.delete(cb.value); cb.closest('label').classList.remove('checked');
  });
  updateProjectTriggerLabel(); updateURL(); applyFilter(); fetchTimeline();
}

// ── URL persistence ────────────────────────────────────────────────────────
function updateURL() {
  const allModels = Array.from(document.querySelectorAll('#model-checkboxes input')).map(cb => cb.value);
  const params = new URLSearchParams();
  if (selectedRange !== '30d') params.set('range', selectedRange);
  if (!isDefaultModelSelection(allModels)) params.set('models', Array.from(selectedModels).join(','));
  if (!isDefaultProjectSelection(allProjectsList)) params.set('projects', Array.from(selectedProjects).join(','));
  const search = params.toString() ? '?' + params.toString() : '';
  history.replaceState(null, '', window.location.pathname + search);
}

// ── Session sort ───────────────────────────────────────────────────────────
function setSessionSort(col) {
  if (sessionSortCol === col) {
    sessionSortDir = sessionSortDir === 'desc' ? 'asc' : 'desc';
  } else {
    sessionSortCol = col;
    sessionSortDir = 'desc';
  }
  updateSortIcons();
  applyFilter();
}

function updateSortIcons() {
  document.querySelectorAll('.sort-icon').forEach(el => el.textContent = '');
  const icon = document.getElementById('sort-icon-' + sessionSortCol);
  if (icon) icon.textContent = sessionSortDir === 'desc' ? ' \u25bc' : ' \u25b2';
}

function sortSessions(sessions) {
  return [...sessions].sort((a, b) => {
    let av, bv;
    if (sessionSortCol === 'cost') {
      av = calcCost(a.model, a.input, a.output, a.cache_read, a.cache_creation);
      bv = calcCost(b.model, b.input, b.output, b.cache_read, b.cache_creation);
    } else if (sessionSortCol === 'duration_min') {
      av = parseFloat(a.duration_min) || 0;
      bv = parseFloat(b.duration_min) || 0;
    } else {
      av = a[sessionSortCol] ?? 0;
      bv = b[sessionSortCol] ?? 0;
    }
    if (av < bv) return sessionSortDir === 'desc' ? 1 : -1;
    if (av > bv) return sessionSortDir === 'desc' ? -1 : 1;
    return 0;
  });
}

// ── Aggregation & filtering ────────────────────────────────────────────────
// ── Timeline cascading selection ────────────────────────────────────────────
// A drag-selection on the Usage Timeline chart narrows every chart/table below
// it, on top of the model/project/range filter bar above it. null = no
// selection active (only the filter bar's range/model/project apply).
let timelineSelection = null;  // { start: bucketStr, end: bucketStr } at timelineGranularity's resolution

// Does a "day"+"hour" pair (the resolution the model/subagent/skill/tool
// breakdowns carry) fall inside the active Timeline selection?
function withinTimelineSelectionDayHour(day, hour) {
  if (!timelineSelection) return true;
  const { start, end } = timelineSelection;
  if (timelineGranularity === 'day') return day >= start && day <= end;
  // Hour/minute granularity: these breakdowns only carry hour resolution, so
  // a minute-level selection is approximated to its enclosing hour range.
  // ponytail: true minute precision here would need per-minute backend
  // queries for these datasets -- upgrade if that granularity is ever needed
  // for the model/subagent/skill/tool views specifically.
  const key = day + ' ' + String(hour ?? 0).padStart(2, '0') + ':00';
  return key >= start.slice(0, 13) + ':00' && key <= end.slice(0, 13) + ':00';
}

// Does a "YYYY-MM-DD HH:MM" timestamp (sessions/dispatches -- minute precision
// already) fall inside the active Timeline selection? True precision here,
// unlike the day-hour version above.
function withinTimelineSelectionMinute(ts) {
  if (!timelineSelection) return true;
  const { start, end } = timelineSelection;
  if (timelineGranularity === 'day') return ts.slice(0, 10) >= start && ts.slice(0, 10) <= end;
  if (timelineGranularity === 'hour') return ts.slice(0, 13) >= start.slice(0, 13) && ts.slice(0, 13) <= end.slice(0, 13);
  return ts >= start && ts <= end;
}

// Below-the-fold filtering: everything from the Usage Timeline chart down.
// The top-3 fixed sections (key metrics, Daily, Average Hourly) are rendered
// separately by renderTopFixed() and never touch selectedModels/Projects/
// Range/timelineSelection -- see that function.
function applyFilter() {
  if (!rawData) return;

  const { start, end } = getRangeBounds(selectedRange);

  // By model: hourly_by_model carries day+hour+model+project, so it doubles
  // as the source for both the plain day-range filter and the Timeline's
  // hour/minute cascading selection (daily_by_model is only used by the
  // fixed top-3 Daily chart now -- see renderTopFixed).
  const filteredHourly = (rawData.hourly_by_model || []).filter(r =>
    selectedModels.has(r.model) && selectedProjects.has(r.project) &&
    (!start || r.day >= start) && (!end || r.day <= end) &&
    withinTimelineSelectionDayHour(r.day, r.hour)
  );

  const modelMap = {};
  for (const r of filteredHourly) {
    if (!modelMap[r.model]) modelMap[r.model] = { model: r.model, input: 0, output: 0, cache_read: 0, cache_creation: 0, turns: 0, sessions: 0 };
    const m = modelMap[r.model];
    m.input          += r.input;
    m.output         += r.output;
    m.cache_read     += r.cache_read;
    m.cache_creation += r.cache_creation;
    m.turns          += r.turns;
  }

  // Filter sessions by model + project + range + Timeline selection (true
  // minute precision, since sessions carry a minute-precision timestamp).
  const filteredSessions = rawData.sessions_all.filter(s =>
    selectedModels.has(s.model) && selectedProjects.has(s.project) &&
    (!start || s.last_date >= start) && (!end || s.last_date <= end) &&
    withinTimelineSelectionMinute(s.last)
  );

  // Add session counts into modelMap
  for (const s of filteredSessions) {
    if (modelMap[s.model]) modelMap[s.model].sessions++;
  }

  const byModel = Object.values(modelMap).sort((a, b) => (b.input + b.output) - (a.input + a.output));

  // By project: aggregate from filtered sessions
  const projMap = {};
  for (const s of filteredSessions) {
    if (!projMap[s.project]) projMap[s.project] = { project: s.project, input: 0, output: 0, cache_read: 0, cache_creation: 0, turns: 0, sessions: 0, cost: 0 };
    const p = projMap[s.project];
    p.input          += s.input;
    p.output         += s.output;
    p.cache_read     += s.cache_read;
    p.cache_creation += s.cache_creation;
    p.turns          += s.turns;
    p.sessions++;
    p.cost += calcCost(s.model, s.input, s.output, s.cache_read, s.cache_creation);
  }
  const byProject = Object.values(projMap).sort((a, b) => (b.input + b.output) - (a.input + a.output));

  // By skill. Cost is summed per source row (day+hour+skill+model+project)
  // rather than computed once on the aggregate, since a skill can be invoked
  // under different models with different pricing.
  const skillMap = {};
  for (const r of (rawData.skill_by_day || [])) {
    if (!selectedModels.has(r.model) || !selectedProjects.has(r.project)) continue;
    if (start && r.day < start) continue;
    if (end && r.day > end) continue;
    if (!withinTimelineSelectionDayHour(r.day, r.hour)) continue;
    const k = r.skill;
    if (!skillMap[k]) skillMap[k] = { skill: k, input: 0, output: 0, cache_read: 0, cache_creation: 0, turns: 0, cost: 0 };
    const m = skillMap[k];
    m.input += r.input; m.output += r.output;
    m.cache_read += r.cache_read; m.cache_creation += r.cache_creation;
    m.turns += r.turns;
    m.cost += calcCost(r.model, r.input, r.output, r.cache_read, r.cache_creation);
  }
  const bySkill = Object.values(skillMap).sort((a, b) => b.cost - a.cost);

  // By tool -- MCP servers collapsed to "MCP: <server>", Bash calls collapsed
  // to "CLI: <name>" (see toolGroupLabel). Same per-row cost summing as
  // bySkill above, for the same reason.
  const toolMap = {};
  for (const r of (rawData.tool_by_day || [])) {
    if (!selectedModels.has(r.model) || !selectedProjects.has(r.project)) continue;
    if (start && r.day < start) continue;
    if (end && r.day > end) continue;
    if (!withinTimelineSelectionDayHour(r.day, r.hour)) continue;
    const k = toolGroupLabel(r.tool_name, r.cli_name);
    if (!toolMap[k]) toolMap[k] = { tool: k, input: 0, output: 0, cache_read: 0, cache_creation: 0, turns: 0, cost: 0 };
    const m = toolMap[k];
    m.input += r.input; m.output += r.output;
    m.cache_read += r.cache_read; m.cache_creation += r.cache_creation;
    m.turns += r.turns;
    m.cost += calcCost(r.model, r.input, r.output, r.cache_read, r.cache_creation);
  }
  const byTool = Object.values(toolMap).sort((a, b) => b.cost - a.cost);

  // Subagent breakdown by type
  const subagentTypeMap = {};
  for (const r of (rawData.subagent_by_type || [])) {
    if (!selectedModels.has(r.model) || !selectedProjects.has(r.project)) continue;
    if (start && r.day < start) continue;
    if (end && r.day > end) continue;
    if (!withinTimelineSelectionDayHour(r.day, r.hour)) continue;
    const k = r.agent_type;
    if (!subagentTypeMap[k]) subagentTypeMap[k] = { agent_type: k, input: 0, output: 0, cache_read: 0, cache_creation: 0, turns: 0 };
    const m = subagentTypeMap[k];
    m.input += r.input; m.output += r.output;
    m.cache_read += r.cache_read; m.cache_creation += r.cache_creation;
    m.turns += r.turns;
  }
  const byAgentType = Object.values(subagentTypeMap).sort((a, b) =>
    (b.input + b.output + b.cache_read + b.cache_creation) -
    (a.input + a.output + a.cache_read + a.cache_creation));

  // Top dispatches: filter by range/model/project + Timeline selection (true
  // minute precision, since dispatches carry a minute-precision start time).
  // Keep the full filtered set (already ranked by tokens server-side) so the
  // table can page it like Recent Sessions -- show more/less plus CSV export.
  const filteredDispatches = (rawData.top_dispatches || []).filter(d =>
    selectedModels.has(d.model) && selectedProjects.has(d.project) &&
    (!start || d.start_date >= start) && (!end || d.start_date <= end) &&
    withinTimelineSelectionMinute(d.start)
  );

  document.getElementById('subagent-chart-title').textContent = 'Subagent Tokens by Type — ' + RANGE_LABELS[selectedRange];

  renderModelChart(byModel);
  renderProjectChart(byProject);
  renderSubagentChart(byAgentType);
  refilterTimeline();  // cheap client-side re-filter of already-fetched timeline rows
  lastFilteredDispatches = filteredDispatches;
  renderTopDispatches(lastFilteredDispatches);
  lastFilteredSessions = sortSessions(filteredSessions);
  lastByModel = byModel;
  lastByProject = sortProjects(byProject);
  lastBySkill = sortSkills(bySkill);
  lastByTool = sortTools(byTool);
  renderSessionsTable(lastFilteredSessions);
  renderModelCostTable(lastByModel);
  renderProjectCostTable(lastByProject);
  renderSkillCostTable(lastBySkill);
  renderToolCostTable(lastByTool);
}

// ── Top-3 fixed sections (key metrics, Daily, Average Hourly) ──────────────
// Always the last 30 days, all models, all projects -- completely independent
// of the filter bar / Timeline selection below. Called once per data load
// (initial + each auto-refresh poll), never from applyFilter().
function renderTopFixed(d) {
  const { start, end } = getRangeBounds('30d');

  const filteredDaily = (d.daily_by_model || []).filter(r =>
    (!start || r.day >= start) && (!end || r.day <= end)
  );

  const dailyMap = {};
  for (const r of filteredDaily) {
    if (!dailyMap[r.day]) dailyMap[r.day] = { day: r.day, input: 0, output: 0, cache_read: 0, cache_creation: 0 };
    const m = dailyMap[r.day];
    m.input += r.input; m.output += r.output;
    m.cache_read += r.cache_read; m.cache_creation += r.cache_creation;
  }
  const daily = Object.values(dailyMap).sort((a, b) => a.day.localeCompare(b.day));

  const modelMap = {};
  for (const r of filteredDaily) {
    if (!modelMap[r.model]) modelMap[r.model] = { model: r.model, input: 0, output: 0, cache_read: 0, cache_creation: 0, turns: 0 };
    const m = modelMap[r.model];
    m.input += r.input; m.output += r.output;
    m.cache_read += r.cache_read; m.cache_creation += r.cache_creation;
    m.turns += r.turns;
  }
  const byModel = Object.values(modelMap);

  const filteredSessions = (d.sessions_all || []).filter(s =>
    (!start || s.last_date >= start) && (!end || s.last_date <= end)
  );

  const totals = {
    sessions:       filteredSessions.length,
    turns:          byModel.reduce((s, m) => s + m.turns, 0),
    input:          byModel.reduce((s, m) => s + m.input, 0),
    output:         byModel.reduce((s, m) => s + m.output, 0),
    cache_read:     byModel.reduce((s, m) => s + m.cache_read, 0),
    cache_creation: byModel.reduce((s, m) => s + m.cache_creation, 0),
    cost:           byModel.reduce((s, m) => s + calcCost(m.model, m.input, m.output, m.cache_read, m.cache_creation), 0),
    subagent_tokens: (d.subagent_by_type || [])
      .filter(r => (!start || r.day >= start) && (!end || r.day <= end))
      .reduce((s, r) => s + r.input + r.output + r.cache_read + r.cache_creation, 0),
  };

  const hourlySrc = (d.hourly_by_model || []).filter(r =>
    (!start || r.day >= start) && (!end || r.day <= end)
  );
  const hourlyAgg = aggregateHourly(hourlySrc, hourlyTZ);

  renderStats(totals);
  renderDailyChart(daily);
  renderHourlyChart(hourlyAgg);
}

// ── Renderers ──────────────────────────────────────────────────────────────
function renderStats(t) {
  // Fixed label — renderTopFixed() is the only caller, always a 30-day window.
  const rangeLabel = 'last 30 days';
  const stats = [
    { label: 'Sessions',       value: t.sessions.toLocaleString(), sub: rangeLabel },
    { label: 'Turns',          value: fmt(t.turns),                sub: rangeLabel },
    { label: 'Input Tokens',   value: fmt(t.input),                sub: rangeLabel },
    { label: 'Output Tokens',  value: fmt(t.output),               sub: rangeLabel },
    { label: 'Subagent Tokens', value: fmt(t.subagent_tokens || 0), sub: 'included in totals' },
    { label: 'Cache Read',     value: fmt(t.cache_read),           sub: 'from prompt cache' },
    { label: 'Cache Creation', value: fmt(t.cache_creation),       sub: 'writes to prompt cache' },
    { label: 'Est. Cost',      value: fmtCostBig(t.cost),          sub: 'API pricing, June 2026', color: C.green },
  ];
  document.getElementById('stats-row').innerHTML = stats.map(s => `
    <div class="stat-card">
      <div class="label">${s.label}</div>
      <div class="value" style="${s.color ? 'color:' + s.color : ''}">${esc(s.value)}</div>
      ${s.sub ? `<div class="sub">${esc(s.sub)}</div>` : ''}
    </div>
  `).join('');
}

// Bucket rows into 24 hours (display-TZ), summing turns + output, and count
// the unique days in the input so the caller can compute per-day averages.
function aggregateHourly(rows, tzMode) {
  const byHour = {};
  for (let h = 0; h < 24; h++) byHour[h] = { turns: 0, output: 0 };
  const days = new Set();
  for (const r of rows) {
    const displayHour = utcHourToDisplay(r.hour, tzMode);
    byHour[displayHour].turns  += r.turns  || 0;
    byHour[displayHour].output += r.output || 0;
    if (r.day) days.add(r.day);
  }
  const dayCount = days.size;
  const hours = [];
  for (let h = 0; h < 24; h++) {
    hours.push({
      hour:       h,
      avgTurns:   dayCount ? byHour[h].turns  / dayCount : 0,
      avgOutput:  dayCount ? byHour[h].output / dayCount : 0,
      totalTurns: byHour[h].turns,
      peak:       isPeakHour(h, tzMode),
    });
  }
  return { hours, dayCount };
}

function renderHourlyChart(agg) {
  const dayCountEl = document.getElementById('hourly-day-count');
  dayCountEl.textContent = agg.dayCount
    ? agg.dayCount + ' day' + (agg.dayCount === 1 ? '' : 's') + ' averaged · ' + tzDisplayName(hourlyTZ)
    : 'No data · ' + tzDisplayName(hourlyTZ);

  const ctx = document.getElementById('chart-hourly').getContext('2d');
  if (charts.hourly) charts.hourly.destroy();

  const labels = agg.hours.map(h => formatHourLabel(h.hour));
  const turns  = agg.hours.map(h => h.avgTurns);
  const output = agg.hours.map(h => h.avgOutput);
  const barColors      = agg.hours.map(h => h.peak ? 'rgba(199,78,57,0.9)' : TOKEN_COLORS.input);
  const barHoverColors = agg.hours.map(h => h.peak ? 'rgba(199,78,57,1)'   : TOKEN_HOVER.input);

  charts.hourly = new Chart(ctx, {
    data: {
      labels: labels,
      datasets: [
        {
          type: 'bar',
          label: 'Avg turns / hour',
          hidden: hiddenSeries.hourly.has('Avg turns / hour'),
          data: turns,
          backgroundColor: barColors,
          hoverBackgroundColor: barHoverColors,
          pointStyle: 'rect',
          yAxisID: 'y',
          order: 2,
        },
        {
          type: 'line',
          label: 'Avg output tokens / hour',
          hidden: hiddenSeries.hourly.has('Avg output tokens / hour'),
          data: output,
          borderColor: TOKEN_COLORS.output,
          backgroundColor: 'rgba(217,119,87,0.15)',
          borderWidth: 2,
          pointRadius: 2,
          pointHoverRadius: 4,
          pointHoverBackgroundColor: TOKEN_HOVER.output,
          pointStyle: 'circle',
          pointBackgroundColor: TOKEN_COLORS.output,
          pointBorderColor: TOKEN_COLORS.output,
          tension: 0.3,
          yAxisID: 'y1',
          order: 1,
        },
      ]
    },
    options: {
      responsive: true, maintainAspectRatio: false, resizeDelay: 150,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { onClick: legendToggle('hourly'), labels: { color: C.axis, usePointStyle: true, boxWidth: 8, boxHeight: 8 } },
        tooltip: {
          usePointStyle: true,
          callbacks: {
            title: (items) => {
              if (!items.length) return '';
              const idx = items[0].dataIndex;
              const h = agg.hours[idx];
              const base = formatHourLabel(h.hour) + ' ' + tzDisplayName(hourlyTZ);
              return h.peak ? base + ' · Peak — Anthropic US hours' : base;
            },
            label: (item) => {
              if (item.dataset.label && item.dataset.label.indexOf('turns') !== -1) {
                return ' Avg turns: ' + item.parsed.y.toFixed(2);
              }
              return ' Avg output: ' + fmt(item.parsed.y);
            },
          }
        },
      },
      scales: {
        x: { ticks: { color: C.axis, maxRotation: 0, autoSkip: false, font: { size: 10 } }, grid: { color: C.border } },
        y:  { position: 'left',  beginAtZero: true, ticks: { color: C.axis, callback: v => v.toFixed(1) },     grid: { color: C.border }, title: { display: true, text: 'Avg turns / hour',         color: C.axis, font: { size: 11 } } },
        y1: { position: 'right', beginAtZero: true, ticks: { color: C.axis, callback: v => fmt(v) }, grid: { drawOnChartArea: false },   title: { display: true, text: 'Avg output tokens / hour', color: C.axis, font: { size: 11 } } },
      }
    }
  });
}

function renderDailyChart(daily) {
  const ctx = document.getElementById('chart-daily').getContext('2d');
  if (charts.daily) charts.daily.destroy();
  charts.daily = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: daily.map(d => d.day),
      datasets: [
        { label: 'Input',          hidden: hiddenSeries.daily.has('Input'),          data: daily.map(d => d.input),          backgroundColor: TOKEN_COLORS.input,          hoverBackgroundColor: TOKEN_HOVER.input,          stack: 'io',    yAxisID: 'y1' },
        { label: 'Output',         hidden: hiddenSeries.daily.has('Output'),         data: daily.map(d => d.output),         backgroundColor: TOKEN_COLORS.output,         hoverBackgroundColor: TOKEN_HOVER.output,         stack: 'io',    yAxisID: 'y1' },
        { label: 'Cache Read',     hidden: hiddenSeries.daily.has('Cache Read'),     data: daily.map(d => d.cache_read),     backgroundColor: TOKEN_COLORS.cache_read,     hoverBackgroundColor: TOKEN_HOVER.cache_read,     stack: 'cache', yAxisID: 'y' },
        { label: 'Cache Creation', hidden: hiddenSeries.daily.has('Cache Creation'), data: daily.map(d => d.cache_creation), backgroundColor: TOKEN_COLORS.cache_creation, hoverBackgroundColor: TOKEN_HOVER.cache_creation, stack: 'cache', yAxisID: 'y' },
      ]
    },
    options: {
      responsive: true, maintainAspectRatio: false, resizeDelay: 150,
      plugins: { legend: { onClick: legendToggle('daily'), labels: { color: C.axis, boxWidth: 12 } } },
      scales: {
        x: { ticks: { color: C.axis, maxTicksLimit: RANGE_TICKS[selectedRange] }, grid: { color: C.border } },
        y:  { position: 'left',  ticks: { color: C.green, callback: v => fmt(v) }, grid: { color: C.border }, title: { display: true, text: 'Cache', color: C.green } },
        y1: { position: 'right', ticks: { color: C.blue, callback: v => fmt(v) }, grid: { drawOnChartArea: false },    title: { display: true, text: 'Input / Output', color: C.blue } },
      }
    }
  });
}

function renderModelChart(byModel) {
  const ctx = document.getElementById('chart-model').getContext('2d');
  if (charts.model) charts.model.destroy();
  if (!byModel.length) { charts.model = null; return; }
  charts.model = new Chart(ctx, {
    type: 'doughnut',
    data: {
      labels: byModel.map(m => m.model),
      datasets: [{ data: byModel.map(m => m.input + m.output), backgroundColor: MODEL_COLORS, hoverBackgroundColor: MODEL_COLORS, hoverOffset: 8, borderWidth: 2, borderColor: C.card, hoverBorderColor: C.card }]
    },
    options: {
      responsive: true, maintainAspectRatio: false, resizeDelay: 150,
      plugins: {
        legend: {
          position: 'bottom',
          labels: { color: C.axis, boxWidth: 12, font: { size: 11 } },
          onClick: (e, item, legend) => {
            const ci = legend.chart;
            ci.toggleDataVisibility(item.index);
            const label = ci.data.labels[item.index];
            if (!ci.getDataVisibility(item.index)) hiddenSeries.model.add(label); else hiddenSeries.model.delete(label);
            ci.update();
          },
        },
        tooltip: { callbacks: { label: ctx => ` ${ctx.label}: ${fmt(ctx.raw)} tokens` } }
      }
    }
  });
  // Reapply any slices the user toggled off in a previous render.
  byModel.forEach((m, i) => {
    if (hiddenSeries.model.has(m.model) && charts.model.getDataVisibility(i)) charts.model.toggleDataVisibility(i);
  });
  charts.model.update();
}

function renderProjectChart(byProject) {
  const top = byProject.slice(0, 10);
  const ctx = document.getElementById('chart-project').getContext('2d');
  if (charts.project) charts.project.destroy();
  if (!top.length) { charts.project = null; return; }
  charts.project = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: top.map(p => p.project.length > 22 ? '\u2026' + p.project.slice(-20) : p.project),
      datasets: [
        { label: 'Input',  hidden: hiddenSeries.project.has('Input'),  data: top.map(p => p.input),  backgroundColor: TOKEN_COLORS.input,  hoverBackgroundColor: TOKEN_HOVER.input },
        { label: 'Output', hidden: hiddenSeries.project.has('Output'), data: top.map(p => p.output), backgroundColor: TOKEN_COLORS.output, hoverBackgroundColor: TOKEN_HOVER.output },
      ]
    },
    options: {
      indexAxis: 'y', responsive: true, maintainAspectRatio: false, resizeDelay: 150,
      plugins: { legend: { onClick: legendToggle('project'), labels: { color: C.axis, boxWidth: 12 } } },
      scales: {
        x: { ticks: { color: C.axis, callback: v => fmt(v) }, grid: { color: C.border } },
        y: { ticks: { color: C.axis, font: { size: 11 } }, grid: { color: C.border } },
      }
    }
  });
}

function renderSubagentChart(byType) {
  const ctx = document.getElementById('chart-subagent').getContext('2d');
  if (charts.subagent) charts.subagent.destroy();
  if (!byType.length) { charts.subagent = null; return; }
  charts.subagent = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: byType.map(t => t.agent_type),
      datasets: [
        { label: 'Input',          hidden: hiddenSeries.subagent.has('Input'),          data: byType.map(t => t.input),          backgroundColor: TOKEN_COLORS.input,          hoverBackgroundColor: TOKEN_HOVER.input,          stack: 'tokens' },
        { label: 'Output',         hidden: hiddenSeries.subagent.has('Output'),         data: byType.map(t => t.output),         backgroundColor: TOKEN_COLORS.output,         hoverBackgroundColor: TOKEN_HOVER.output,         stack: 'tokens' },
        { label: 'Cache Read',     hidden: hiddenSeries.subagent.has('Cache Read'),     data: byType.map(t => t.cache_read),     backgroundColor: TOKEN_COLORS.cache_read,     hoverBackgroundColor: TOKEN_HOVER.cache_read,     stack: 'tokens' },
        { label: 'Cache Creation', hidden: hiddenSeries.subagent.has('Cache Creation'), data: byType.map(t => t.cache_creation), backgroundColor: TOKEN_COLORS.cache_creation, hoverBackgroundColor: TOKEN_HOVER.cache_creation, stack: 'tokens' },
      ]
    },
    options: {
      indexAxis: 'y', responsive: true, maintainAspectRatio: false, resizeDelay: 150,
      plugins: {
        legend: { onClick: legendToggle('subagent'), labels: { color: C.axis, boxWidth: 12 } },
        tooltip: { callbacks: {
          label: ctx => ` ${ctx.dataset.label}: ${fmt(ctx.raw)}`,
          footer: items => {
            const total = items.reduce((s, it) => s + it.raw, 0);
            const row = byType[items[0].dataIndex];
            return ` Total: ${fmt(total)} · ${row.turns} turns`;
          }
        } }
      },
      scales: {
        x: { stacked: true, ticks: { color: C.axis, callback: v => fmt(v) }, grid: { color: C.border } },
        y: { stacked: true, ticks: { color: C.axis, font: { size: 11 } }, grid: { color: C.border } },
      }
    }
  });
}

function renderTopDispatches(rows) {
  const body = document.getElementById('dispatches-body');
  if (!rows.length) {
    body.innerHTML = '<tr><td colspan="11" class="muted" style="text-align:center;padding:24px">No subagent dispatches in selected range.</td></tr>';
    renderTableToggle('dispatches-foot', 0, dispatchesLimit, 'lessDispatchRows', 'moreDispatchRows', 'exportDispatchesCSV');
    return;
  }
  const shown = rows.slice(0, shownCount(dispatchesLimit, rows.length));
  body.innerHTML = shown.map(d => {
    const tokensTotal = d.input + d.output + d.cache_read + d.cache_creation;
    const cost = calcCost(d.model, d.input, d.output, d.cache_read, d.cache_creation);
    const costCell = isBillable(d.model)
      ? `<td class="cost">${fmtCost(cost)}</td>`
      : `<td class="cost-na">n/a</td>`;
    const col = colorForAgentType(d.agent_type);
    const typeStyle = `background:${col}22;color:${col};border:1px solid ${col}44`;
    return `<tr>
      <td><span class="model-tag" style="${typeStyle}">${esc(d.agent_type)}</span></td>
      <td class="muted">${esc(d.start || '—')}</td>
      <td><span class="model-tag">${esc(d.model)}</span></td>
      <td class="num">${d.turns}</td>
      <td class="num">${d.tool_uses != null ? d.tool_uses : '—'}</td>
      <td class="muted">${fmtDuration(d.duration_ms)}</td>
      <td class="num">${fmt(d.input)}</td>
      <td class="num">${fmt(d.output)}</td>
      <td class="num">${fmt(d.cache_read)}</td>
      <td class="num"><strong>${fmt(tokensTotal)}</strong></td>
      ${costCell}
    </tr>`;
  }).join('');
  renderTableToggle('dispatches-foot', rows.length, dispatchesLimit, 'lessDispatchRows', 'moreDispatchRows', 'exportDispatchesCSV');
}

// Fills a table card's footer with the row-reveal control. Three states:
//   - more rows fit under the cap        -> "Show more" (plus "Show less" once expanded)
//   - cap reached but more records exist -> "Download CSV to see all (N)" + "Show less"
//   - every row is already visible       -> "Show less"
// "Show less" is hidden at the initial step (nothing to collapse yet). Renders
// nothing when the whole table fits in the first step. Carets: more = down (▾),
// less = up (▴).
function renderTableToggle(footId, total, limit, lessName, moreName, csvName) {
  const foot = document.getElementById(footId);
  if (!foot) return;
  if (total <= PAGINATE_THRESHOLD) { foot.innerHTML = ''; return; }
  const less = '<button class="show-more-btn" onclick="' + lessName + '()">Show less ▴</button>';
  const more = '<button class="show-more-btn" onclick="' + moreName + '()">Show more ▾</button>';
  let html;
  if (limit < total && limit < TABLE_MAX) {
    // more rows fit under the cap; Show less only once we're past the first step
    html = (limit > TABLE_STEPS[0] ? less : '') + more;
  } else if (limit < total) {           // cap reached, remaining rows only via CSV
    html = '<a class="show-more-link" href="#" onclick="' + csvName + '(); return false;">Download CSV to see all (' + total + ')</a>' + less;
  } else {                              // everything already visible
    html = less;
  }
  foot.innerHTML = html;
}

// After collapsing a table, bring its top back into view — the user may have
// scrolled down through the expanded rows.
function scrollTableToTop(bodyId) {
  const card = document.getElementById(bodyId)?.closest('.table-card');
  if (card) card.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

// "Show more" advances one step (capped at TABLE_MAX); "Show less" resets to the
// first step and scrolls back to the top of that table.
function moreModelRows()   { modelLimit    = nextTableLimit(modelLimit,    lastByModel.length);        renderModelCostTable(lastByModel); }
function lessModelRows()   { modelLimit    = TABLE_STEPS[0]; renderModelCostTable(lastByModel);            scrollTableToTop('model-cost-body'); }
function moreSessionRows() { sessionsLimit = nextTableLimit(sessionsLimit, lastFilteredSessions.length); renderSessionsTable(lastFilteredSessions); }
function lessSessionRows() { sessionsLimit = TABLE_STEPS[0]; renderSessionsTable(lastFilteredSessions);    scrollTableToTop('sessions-body'); }
function moreProjectRows() { projectLimit  = nextTableLimit(projectLimit,  lastByProject.length);       renderProjectCostTable(lastByProject); }
function lessProjectRows() { projectLimit  = TABLE_STEPS[0]; renderProjectCostTable(lastByProject);        scrollTableToTop('project-cost-body'); }
function moreSkillRows()   { skillLimit    = nextTableLimit(skillLimit,    lastBySkill.length);        renderSkillCostTable(lastBySkill); }
function lessSkillRows()   { skillLimit    = TABLE_STEPS[0]; renderSkillCostTable(lastBySkill);            scrollTableToTop('skill-cost-body'); }
function moreToolRows()    { toolLimit     = nextTableLimit(toolLimit,     lastByTool.length);         renderToolCostTable(lastByTool); }
function lessToolRows()    { toolLimit     = TABLE_STEPS[0]; renderToolCostTable(lastByTool);              scrollTableToTop('tool-cost-body'); }
function moreDispatchRows(){ dispatchesLimit = nextTableLimit(dispatchesLimit, lastFilteredDispatches.length); renderTopDispatches(lastFilteredDispatches); }
function lessDispatchRows(){ dispatchesLimit = TABLE_STEPS[0]; renderTopDispatches(lastFilteredDispatches);            scrollTableToTop('dispatches-body'); }

function renderSessionsTable(sessions) {
  const shown = sessions.slice(0, shownCount(sessionsLimit, sessions.length));
  document.getElementById('sessions-body').innerHTML = shown.map(s => {
    const cost = calcCost(s.model, s.input, s.output, s.cache_read, s.cache_creation);
    const costCell = isBillable(s.model)
      ? `<td class="cost">${fmtCost(cost)}</td>`
      : `<td class="cost-na">n/a</td>`;
    const titleCell = s.topic
      ? `<td class="topic-cell" title="${esc(s.topic)}">${esc(s.topic)}</td>`
      : `<td class="topic-cell"><span class="untitled">Untitled</span></td>`;
    return `<tr>
      <td class="muted" style="font-family:monospace">${esc(s.session_id.slice(0, 8))}&hellip;</td>
      <td>${esc(s.project)}</td>
      ${titleCell}
      <td class="muted">${esc(s.last)}</td>
      <td class="muted">${esc(s.duration_min)}m</td>
      <td><span class="model-tag">${esc(s.model)}</span></td>
      <td class="num">${s.turns}</td>
      <td class="num">${fmt(s.input)}</td>
      <td class="num">${fmt(s.output)}</td>
      ${costCell}
    </tr>`;
  }).join('');
  renderTableToggle('sessions-foot', sessions.length, sessionsLimit, 'lessSessionRows', 'moreSessionRows', 'exportSessionsCSV');
}

function setModelSort(col) {
  if (modelSortCol === col) {
    modelSortDir = modelSortDir === 'desc' ? 'asc' : 'desc';
  } else {
    modelSortCol = col;
    modelSortDir = 'desc';
  }
  updateModelSortIcons();
  applyFilter();
}

function updateModelSortIcons() {
  document.querySelectorAll('[id^="msort-"]').forEach(el => el.textContent = '');
  const icon = document.getElementById('msort-' + modelSortCol);
  if (icon) icon.textContent = modelSortDir === 'desc' ? ' \u25bc' : ' \u25b2';
}

function sortModels(byModel) {
  return [...byModel].sort((a, b) => {
    let av, bv;
    if (modelSortCol === 'cost') {
      av = calcCost(a.model, a.input, a.output, a.cache_read, a.cache_creation);
      bv = calcCost(b.model, b.input, b.output, b.cache_read, b.cache_creation);
    } else {
      av = a[modelSortCol] ?? 0;
      bv = b[modelSortCol] ?? 0;
    }
    if (av < bv) return modelSortDir === 'desc' ? 1 : -1;
    if (av > bv) return modelSortDir === 'desc' ? -1 : 1;
    return 0;
  });
}

function renderModelCostTable(byModel) {
  const sorted = sortModels(byModel);
  const shown = sorted.slice(0, shownCount(modelLimit, sorted.length));
  document.getElementById('model-cost-body').innerHTML = shown.map(m => {
    const cost = calcCost(m.model, m.input, m.output, m.cache_read, m.cache_creation);
    const costCell = isBillable(m.model)
      ? `<td class="cost">${fmtCost(cost)}</td>`
      : `<td class="cost-na">n/a</td>`;
    return `<tr>
      <td><span class="model-tag">${esc(m.model)}</span></td>
      <td class="num">${fmt(m.turns)}</td>
      <td class="num">${fmt(m.input)}</td>
      <td class="num">${fmt(m.output)}</td>
      <td class="num">${fmt(m.cache_read)}</td>
      <td class="num">${fmt(m.cache_creation)}</td>
      ${costCell}
    </tr>`;
  }).join('');
  renderTableToggle('model-cost-foot', sorted.length, modelLimit, 'lessModelRows', 'moreModelRows', 'exportModelCSV');
}

// ── Project cost table sorting ────────────────────────────────────────────
function setProjectSort(col) {
  if (projectSortCol === col) {
    projectSortDir = projectSortDir === 'desc' ? 'asc' : 'desc';
  } else {
    projectSortCol = col;
    projectSortDir = 'desc';
  }
  updateProjectSortIcons();
  applyFilter();
}

function updateProjectSortIcons() {
  document.querySelectorAll('[id^="psort-"]').forEach(el => el.textContent = '');
  const icon = document.getElementById('psort-' + projectSortCol);
  if (icon) icon.textContent = projectSortDir === 'desc' ? ' \u25bc' : ' \u25b2';
}

function sortProjects(byProject) {
  return [...byProject].sort((a, b) => {
    const av = a[projectSortCol] ?? 0;
    const bv = b[projectSortCol] ?? 0;
    if (av < bv) return projectSortDir === 'desc' ? 1 : -1;
    if (av > bv) return projectSortDir === 'desc' ? -1 : 1;
    return 0;
  });
}

function renderProjectCostTable(byProject) {
  const sorted = sortProjects(byProject);
  const shown = sorted.slice(0, shownCount(projectLimit, sorted.length));
  document.getElementById('project-cost-body').innerHTML = shown.map(p => {
    return `<tr>
      <td>${esc(p.project)}</td>
      <td class="num">${p.sessions}</td>
      <td class="num">${fmt(p.turns)}</td>
      <td class="num">${fmt(p.input)}</td>
      <td class="num">${fmt(p.output)}</td>
      <td class="cost">${fmtCost(p.cost)}</td>
    </tr>`;
  }).join('');
  renderTableToggle('project-cost-foot', sorted.length, projectLimit, 'lessProjectRows', 'moreProjectRows', 'exportProjectsCSV');
}

// ── Skill cost table sorting ─────────────────────────────
function setSkillSort(col) {
  if (skillSortCol === col) {
    skillSortDir = skillSortDir === 'desc' ? 'asc' : 'desc';
  } else {
    skillSortCol = col;
    skillSortDir = 'desc';
  }
  updateSkillSortIcons();
  applyFilter();
}

function updateSkillSortIcons() {
  document.querySelectorAll('[id^="sksort-"]').forEach(el => el.textContent = '');
  const icon = document.getElementById('sksort-' + skillSortCol);
  if (icon) icon.textContent = skillSortDir === 'desc' ? ' ▼' : ' ▲';
}

function sortSkills(rows) {
  return [...rows].sort((a, b) => {
    const av = a[skillSortCol] ?? 0;
    const bv = b[skillSortCol] ?? 0;
    if (av < bv) return skillSortDir === 'desc' ? 1 : -1;
    if (av > bv) return skillSortDir === 'desc' ? -1 : 1;
    return 0;
  });
}

function renderSkillCostTable(rows) {
  const sorted = sortSkills(rows);
  const shown = sorted.slice(0, shownCount(skillLimit, sorted.length));
  document.getElementById('skill-cost-body').innerHTML = shown.length ? shown.map(sk => {
    return `<tr>
      <td>${esc(sk.skill)}</td>
      <td class="num">${fmt(sk.turns)}</td>
      <td class="num">${fmt(sk.input)}</td>
      <td class="num">${fmt(sk.output)}</td>
      <td class="num">${fmt(sk.cache_read)}</td>
      <td class="num">${fmt(sk.cache_creation)}</td>
      <td class="cost">${fmtCost(sk.cost)}</td>
    </tr>`;
  }).join('') : '<tr><td colspan="7" class="muted">No skill invocations in range.</td></tr>';
  renderTableToggle('skill-cost-foot', sorted.length, skillLimit, 'lessSkillRows', 'moreSkillRows', 'exportSkillsCSV');
}

// ── Tool (MCP / CLI) cost table sorting ───────────────────────
// mcp__<server>__<tool> collapses to "MCP: <server>"; everything else (Bash,
// Read, Edit, Skill, ...) is shown as its own tool name. Tolerates a server
// name that itself contains "__" by treating everything between the first
// and last segment as the server.
function toolGroupLabel(toolName, cliName) {
  if (!toolName) return 'unknown';
  if (toolName.startsWith('mcp__')) {
    const parts = toolName.split('__');
    return parts.length >= 3 ? 'MCP: ' + parts.slice(1, -1).join('__') : 'MCP: unknown';
  }
  if (toolName === 'Bash' && cliName) return 'CLI: ' + cliName;
  return toolName;
}

function setToolSort(col) {
  if (toolSortCol === col) {
    toolSortDir = toolSortDir === 'desc' ? 'asc' : 'desc';
  } else {
    toolSortCol = col;
    toolSortDir = 'desc';
  }
  updateToolSortIcons();
  applyFilter();
}

function updateToolSortIcons() {
  document.querySelectorAll('[id^="tlsort-"]').forEach(el => el.textContent = '');
  const icon = document.getElementById('tlsort-' + toolSortCol);
  if (icon) icon.textContent = toolSortDir === 'desc' ? ' ▼' : ' ▲';
}

function sortTools(rows) {
  return [...rows].sort((a, b) => {
    const av = a[toolSortCol] ?? 0;
    const bv = b[toolSortCol] ?? 0;
    if (av < bv) return toolSortDir === 'desc' ? 1 : -1;
    if (av > bv) return toolSortDir === 'desc' ? -1 : 1;
    return 0;
  });
}

function renderToolCostTable(rows) {
  const sorted = sortTools(rows);
  const shown = sorted.slice(0, shownCount(toolLimit, sorted.length));
  document.getElementById('tool-cost-body').innerHTML = shown.length ? shown.map(t => {
    return `<tr>
      <td>${esc(t.tool)}</td>
      <td class="num">${fmt(t.turns)}</td>
      <td class="num">${fmt(t.input)}</td>
      <td class="num">${fmt(t.output)}</td>
      <td class="num">${fmt(t.cache_read)}</td>
      <td class="num">${fmt(t.cache_creation)}</td>
      <td class="cost">${fmtCost(t.cost)}</td>
    </tr>`;
  }).join('') : '<tr><td colspan="7" class="muted">No tool usage in range.</td></tr>';
  renderTableToggle('tool-cost-foot', sorted.length, toolLimit, 'lessToolRows', 'moreToolRows', 'exportToolsCSV');
}

// ── CSV Export ────────────────────────────────────────────────────────────
function csvField(val) {
  const s = String(val);
  if (s.includes(',') || s.includes('"') || s.includes('\n')) {
    return '"' + s.replace(/"/g, '""') + '"';
  }
  return s;
}

function csvTimestamp() {
  const d = new Date();
  return d.getFullYear() + '-' + String(d.getMonth()+1).padStart(2,'0') + '-' + String(d.getDate()).padStart(2,'0')
    + '_' + String(d.getHours()).padStart(2,'0') + String(d.getMinutes()).padStart(2,'0');
}

function downloadCSV(reportType, header, rows) {
  const lines = [header.map(csvField).join(',')];
  for (const row of rows) {
    lines.push(row.map(csvField).join(','));
  }
  const blob = new Blob([lines.join('\n')], { type: 'text/csv;charset=utf-8;' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = reportType + '_' + csvTimestamp() + '.csv';
  a.click();
  URL.revokeObjectURL(a.href);
}

function exportModelCSV() {
  const header = ['Model', 'Turns', 'Input', 'Output', 'Cache Read', 'Cache Creation', 'Est. Cost'];
  const rows = sortModels(lastByModel).map(m => {
    const cost = calcCost(m.model, m.input, m.output, m.cache_read, m.cache_creation);
    return [m.model, m.turns, m.input, m.output, m.cache_read, m.cache_creation, cost.toFixed(4)];
  });
  downloadCSV('cost_by_model', header, rows);
}

function exportSessionsCSV() {
  const header = ['Session', 'Project', 'Title', 'Last Active', 'Duration (min)', 'Model', 'Turns', 'Input', 'Output', 'Cache Read', 'Cache Creation', 'Est. Cost'];
  const rows = lastFilteredSessions.map(s => {
    const cost = calcCost(s.model, s.input, s.output, s.cache_read, s.cache_creation);
    return [s.session_id, s.project, s.topic, s.last, s.duration_min, s.model, s.turns, s.input, s.output, s.cache_read, s.cache_creation, cost.toFixed(4)];
  });
  downloadCSV('sessions', header, rows);
}

function exportProjectsCSV() {
  const header = ['Project', 'Sessions', 'Turns', 'Input', 'Output', 'Cache Read', 'Cache Creation', 'Est. Cost'];
  const rows = lastByProject.map(p => {
    return [p.project, p.sessions, p.turns, p.input, p.output, p.cache_read, p.cache_creation, p.cost.toFixed(4)];
  });
  downloadCSV('projects', header, rows);
}

function exportSkillsCSV() {
  const header = ['Skill', 'Turns', 'Input', 'Output', 'Cache Read', 'Cache Creation', 'Est. Cost'];
  const rows = lastBySkill.map(sk => {
    return [sk.skill, sk.turns, sk.input, sk.output, sk.cache_read, sk.cache_creation, sk.cost.toFixed(4)];
  });
  downloadCSV('skills', header, rows);
}

function exportToolsCSV() {
  const header = ['Tool', 'Turns', 'Input', 'Output', 'Cache Read', 'Cache Creation', 'Est. Cost'];
  const rows = lastByTool.map(t => {
    return [t.tool, t.turns, t.input, t.output, t.cache_read, t.cache_creation, t.cost.toFixed(4)];
  });
  downloadCSV('tools', header, rows);
}

function exportDispatchesCSV() {
  const header = ['Type', 'Agent ID', 'Started', 'Model', 'Turns', 'Tool Uses', 'Duration (ms)', 'Input', 'Output', 'Cache Read', 'Cache Creation', 'Total Tokens', 'Est. Cost', 'Status'];
  const rows = lastFilteredDispatches.map(d => {
    const total = d.input + d.output + d.cache_read + d.cache_creation;
    const cost = calcCost(d.model, d.input, d.output, d.cache_read, d.cache_creation);
    return [d.agent_type, d.agent_id, d.start, d.model, d.turns,
            d.tool_uses != null ? d.tool_uses : '', d.duration_ms != null ? d.duration_ms : '',
            d.input, d.output, d.cache_read, d.cache_creation, total, cost.toFixed(4), d.status || ''];
  });
  downloadCSV('subagent_dispatches', header, rows);
}

// ── Rescan ────────────────────────────────────────────────────────────────
async function triggerRescan() {
  const btn = document.getElementById('rescan-btn');
  btn.disabled = true;
  btn.textContent = '\u21bb Scanning...';
  try {
    const resp = await fetch('/api/rescan', { method: 'POST' });
    const d = await resp.json();
    btn.textContent = '\u21bb Rescan (' + d.new + ' new, ' + d.updated + ' updated)';
    await loadData();
  } catch(e) {
    btn.textContent = '\u21bb Rescan (error)';
    console.error(e);
  }
  setTimeout(() => { btn.textContent = '\u21bb Rescan'; btn.disabled = false; }, 3000);
}

// ── Data loading ───────────────────────────────────────────────────────────
async function loadData() {
  try {
    const resp = await fetch('/api/data');
    const d = await resp.json();
    if (d.error) {
      // The server binds and serves before the initial scan finishes, so on a
      // fresh start the DB may not exist yet. Show a non-destructive notice and
      // retry instead of nuking the page — once the background scan creates the
      // DB, the next poll renders normally.
      const meta = document.getElementById('meta');
      if (meta) meta.innerHTML = esc(d.error) + ' — retrying…';
      if (rawData === null) setTimeout(loadData, 3000);
      return;
    }
    const refreshNote = rangeIncludesToday(selectedRange) ? '<br>Auto-refresh in 30s' : '';
    document.getElementById('meta').innerHTML = 'Updated: ' + esc(d.generated_at) + refreshNote;

    const isFirstLoad = rawData === null;
    rawData = d;

    if (isFirstLoad) {
      // Restore range from URL into the dropdown
      selectedRange = readURLRange();
      const rangeSel = document.getElementById('range-select');
      if (rangeSel) rangeSel.value = selectedRange;
      // Timeline granularity's default depends on the restored range, not the
      // '30d' module-load default it was declared with.
      timelineGranularity = defaultTimelineGranularity();
      // Mark default TZ button active
      document.querySelectorAll('.tz-btn').forEach(btn =>
        btn.classList.toggle('active', btn.dataset.tz === hourlyTZ)
      );
      // Build model/project filters (reads URL for selection too)
      buildFilterUI(d.all_models);
      buildProjectFilterUI(d.all_projects || []);
      updateSortIcons();
      updateModelSortIcons();
      updateProjectSortIcons();
      updateSkillSortIcons();
      updateToolSortIcons();
      fetchTimeline();
    }

    renderTopFixed(d);
    applyFilter();
  } catch(e) {
    console.error(e);
  }
}

let autoRefreshTimer = null;
function scheduleAutoRefresh() {
  if (autoRefreshTimer) { clearInterval(autoRefreshTimer); autoRefreshTimer = null; }
  if (rangeIncludesToday(selectedRange)) {
    autoRefreshTimer = setInterval(loadData, 30000);
  }
}

// ── Footer meta: version, extension promo, update check ──────────────────────
// APP_CONFIG is injected server-side (see do_GET). { version, surface }.
const APP_CONFIG = window.APP_CONFIG || { version: '', surface: 'web' };
const REPO_URL = 'https://github.com/phuryn/claude-usage';
const MARKETPLACE_URL = 'https://marketplace.visualstudio.com/items?itemName=PawelHuryn.claude-usage-phuryn';
const UPDATE_CACHE_KEY = 'cu_update_check';
const UPDATE_CACHE_TTL = 24 * 60 * 60 * 1000;  // re-check GitHub at most once a day

// Compare dotted numeric versions ("1.3.0"); leading "v" tolerated. Returns
// true only when `latest` is strictly ahead of `current`.
function isNewer(latest, current) {
  const a = String(latest).replace(/^v/, '').split('.').map(n => parseInt(n, 10) || 0);
  const b = String(current).replace(/^v/, '').split('.').map(n => parseInt(n, 10) || 0);
  for (let i = 0; i < Math.max(a.length, b.length); i++) {
    const x = a[i] || 0, y = b[i] || 0;
    if (x > y) return true;
    if (x < y) return false;
  }
  return false;
}

function appendUpdateLink(latest) {
  const el = document.getElementById('footer-meta');
  if (!el || !el.innerHTML) return;
  const a = document.createElement('a');
  a.className = 'update-link';
  a.href = REPO_URL + '/releases/latest';
  a.target = '_blank';
  a.rel = 'noopener';
  a.textContent = 'Update to v' + latest;
  el.insertAdjacentHTML('beforeend', '&nbsp;&middot;&nbsp;');
  el.appendChild(a);
}

// Web only. Asks GitHub's public releases API whether a newer release exists and,
// if so, appends an "Update to vX.Y.Z" link. Cached in localStorage for 24h and
// fully fail-silent (offline / rate-limited / blocked -> no link, no error). No
// usage data is sent; this is a plain unauthenticated GET of release metadata.
function checkForUpdate(current) {
  let cached = null;
  try { cached = JSON.parse(localStorage.getItem(UPDATE_CACHE_KEY) || 'null'); } catch (e) {}
  if (cached && cached.latest && cached.ts && (Date.now() - cached.ts) < UPDATE_CACHE_TTL) {
    if (isNewer(cached.latest, current)) appendUpdateLink(cached.latest);
    return;
  }
  fetch('https://api.github.com/repos/phuryn/claude-usage/releases/latest', {
    headers: { 'Accept': 'application/vnd.github+json' }
  })
    .then(r => r.ok ? r.json() : null)
    .then(data => {
      if (!data || !data.tag_name) return;
      const latest = String(data.tag_name).replace(/^v/, '');
      try { localStorage.setItem(UPDATE_CACHE_KEY, JSON.stringify({ ts: Date.now(), latest: latest })); } catch (e) {}
      if (isNewer(latest, current)) appendUpdateLink(latest);
    })
    .catch(() => {});  // fail-silent: never let a version check disrupt the dashboard
}

function initFooterMeta() {
  const el = document.getElementById('footer-meta');
  if (!el) return;
  const v = APP_CONFIG.version || '';
  const parts = [];
  if (v) {
    parts.push('Version <a href="' + REPO_URL + '/releases/tag/v' + esc(v) + '" target="_blank" rel="noopener">v' + esc(v) + '</a>');
  }
  // The web build promotes the extension; the embedded build is already in it.
  if (APP_CONFIG.surface !== 'vscode') {
    parts.push('<a href="' + MARKETPLACE_URL + '" target="_blank" rel="noopener">Get the VS Code extension</a>');
  }
  el.innerHTML = parts.join('&nbsp;&middot;&nbsp;');
  // VS Code auto-updates the extension, so only the web build checks for updates.
  if (v && APP_CONFIG.surface !== 'vscode') checkForUpdate(v);
}

// ── Collapsible cards ────────────────────────────────────────────────────────
// Collapsible cards fold away sections you don't use. Collapse state persists
// per card in localStorage and is independent of in-table Show more/less
// (which only pages rows within a single table).
const COLLAPSE_KEY = 'cu_collapsed_cards';

function loadCollapsedSet() {
  try { return new Set(JSON.parse(localStorage.getItem(COLLAPSE_KEY) || '[]')); }
  catch (e) { return new Set(); }
}
function saveCollapsedSet(set) {
  try { localStorage.setItem(COLLAPSE_KEY, JSON.stringify([...set])); } catch (e) {}
}

// Charts created while their card is collapsed (display:none) lay out at zero
// size; resize them once the card is shown again so Chart.js repaints to fit.
function resizeChartsIn(card) {
  card.querySelectorAll('canvas').forEach(cv => {
    const ch = Object.values(charts).find(c => c && c.canvas === cv);
    if (ch) ch.resize();
  });
}

function setCardCollapsed(card, collapsed) {
  card.classList.toggle('collapsed', collapsed);
  const title = card.querySelector('h2, .section-title');
  if (title) title.setAttribute('aria-expanded', String(!collapsed));
}

function toggleCard(card) {
  const collapsed = !card.classList.contains('collapsed');
  setCardCollapsed(card, collapsed);
  const set = loadCollapsedSet();
  if (collapsed) set.add(card.dataset.card); else set.delete(card.dataset.card);
  saveCollapsedSet(set);
  if (!collapsed) requestAnimationFrame(() => resizeChartsIn(card));
}

function initCollapsibleCards() {
  const container = document.querySelector('.container');
  if (!container) return;

  // Restore persisted collapse state + make each title an accessible toggle.
  const collapsed = loadCollapsedSet();
  document.querySelectorAll('[data-card]').forEach(card => {
    const title = card.querySelector('h2, .section-title');
    if (title) {
      title.setAttribute('role', 'button');
      title.setAttribute('tabindex', '0');
      title.title = 'Collapse / expand section';
    }
    setCardCollapsed(card, collapsed.has(card.dataset.card));
  });

  // Toggle a card from its title (caret included). Inner controls (CSV, TZ, sort
  // headers) sit outside the title selector, so they keep their own behaviour.
  const TITLE_SEL = '.chart-card > h2, .chart-header > h2, .table-card > .section-title, .section-header > .section-title';
  const onTitleActivate = (e) => {
    if (e.target.closest('.info-icon')) return;  // info tooltip, not a collapse toggle
    if (e.type === 'keydown') { if (e.key !== 'Enter' && e.key !== ' ') return; e.preventDefault(); }
    const title = e.target.closest(TITLE_SEL);
    const card = title && title.closest('[data-card]');
    if (card) toggleCard(card);
  };
  container.addEventListener('click', onTitleActivate);
  container.addEventListener('keydown', onTitleActivate);
}

initFooterMeta();
initCollapsibleCards();
loadData();
scheduleAutoRefresh();
</script>
</body>
</html>
"""


def find_icon_file():
    """Locate the extension's icon.svg across both run contexts.

    - Bundled in the .vsix: this file lives at ``python/dashboard.py`` and the
      icon is a sibling-of-parent at ``../resources/icon.svg``.
    - Standalone repo (``python cli.py dashboard``): this file is the repo-root
      ``dashboard.py`` and the icon is at ``vscode-extension/resources/icon.svg``.

    Returns the first existing path, or ``None`` so the /icon.svg route can 404
    gracefully (the header ``<img>`` then just renders empty alt text).
    """
    here = Path(__file__).resolve().parent
    for candidate in (
        here.parent / "resources" / "icon.svg",
        here / "vscode-extension" / "resources" / "icon.svg",
    ):
        if candidate.is_file():
            return candidate
    return None


class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        # self.path includes the query string, but every URL the UI emits has
        # one (e.g. "/?range=all"); compare the bare path so bookmarkable
        # URLs don't fall through to 404.
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            # Inject runtime config (version + surface) the page can't know at
            # author time. json.dumps produces a valid JS object literal for the
            # `window.APP_CONFIG = __APP_CONFIG_JSON__;` placeholder in the head.
            config = json.dumps({"version": VERSION, "surface": SURFACE})
            html = HTML_TEMPLATE.replace("__APP_CONFIG_JSON__", config)
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif path == "/api/data":
            # Pass DB_PATH explicitly: get_dashboard_data's default arg is frozen
            # to the original module global at def time, so a bare call would ignore
            # a monkey-patched dashboard.DB_PATH (same contract as /api/rescan). This
            # also keeps the dashboard reading the configured DB rather than a stale
            # path captured at import.
            data = get_dashboard_data(DB_PATH)
            body = json.dumps(data).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif path == "/api/timeline":
            qs = parse_qs(urlparse(self.path).query)
            granularity = qs.get("granularity", ["hour"])[0]
            start = qs.get("start", [None])[0] or None
            end = qs.get("end", [None])[0] or None
            data = get_timeline_data(DB_PATH, granularity=granularity, start=start, end=end)
            body = json.dumps(data).encode("utf-8")
            self.send_response(400 if "error" in data else 200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif path == "/icon.svg":
            icon = find_icon_file()
            if icon is None:
                self.send_response(404)
                self.end_headers()
                return
            body = icon.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "max-age=86400")
            self.end_headers()
            self.wfile.write(body)

        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/rescan":
            # Incremental scan: ingest new/changed JSONL without touching
            # existing rows. The DB is append-only and the only durable store
            # of history once Claude Code prunes old transcripts, so we must
            # never delete it here — scan() dedupes via the message_id index.
            # Pass DB_PATH / DEFAULT_PROJECTS_DIRS explicitly so tests that
            # patch the module globals are honored (scan's defaults are
            # frozen at def time and would otherwise target the real paths).
            import scanner
            db_path = DB_PATH
            result = scanner.scan(
                db_path=db_path,
                projects_dirs=scanner.DEFAULT_PROJECTS_DIRS,
                verbose=False,
            )
            body = json.dumps(result).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()


def serve(host=None, port=None, surface=None):
    global SURFACE
    if surface:
        SURFACE = surface
    host = host or os.environ.get("HOST", "localhost")
    port = port or int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer((host, port), DashboardHandler)
    print(f"Dashboard running at http://{host}:{port}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    serve()
