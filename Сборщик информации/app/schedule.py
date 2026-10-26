"""Windows Task Scheduler integration for the daily digest workflow."""
from __future__ import annotations

import base64
import getpass
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

APP_ROOT = Path(__file__).resolve().parents[1]
TASKS = (
    {"name": "Сборщик информации - Сбор каждый час", "frequency": "hourly",
     "interval_minutes": 60, "arguments": "-m app fetch"},
    {"name": "Сборщик информации - Сбор 12-00", "frequency": "daily",
     "hour": 12, "arguments": "-m app run --notify"},
)
# Retired because publishing is now handled through the MAX browser automation.
RETIRED_TASKS = ("Сборщик информации - Публикация 14-00",)
TASK_DESCRIPTION = "Managed by Сборщик информации. Do not edit manually."
# Windows' stable time-zone identifier. Checking this identity also checks its
# daylight-saving rules; a matching UTC offset alone does not.
MOSCOW_WINDOWS_TIMEZONE = "Russian Standard Time"
# StartWhenAvailable is honored for time-based triggers with an end boundary.
# Far-future end boundary preserves the daily schedule for practical use.
TRIGGER_END_BOUNDARY = "2099-12-31T23:59:59"


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _ensure_windows() -> None:
    if os.name != "nt":
        raise RuntimeError("Команды schedule install/status/remove доступны только в Windows")


def local_timezone_id() -> str | None:
    """Return the Windows time-zone ID, or None if it cannot be read."""
    if os.name != "nt":
        return None
    try:
        result = subprocess.run(
            ["tzutil.exe", "/g"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=10, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    timezone_id = result.stdout.strip()
    return timezone_id or None


def local_timezone_matches_moscow() -> bool:
    """Check the Windows time-zone identity, not only today's UTC offset."""
    return local_timezone_id() == MOSCOW_WINDOWS_TIMEZONE


def _ensure_moscow_timezone() -> None:
    timezone_id = local_timezone_id()
    if timezone_id == MOSCOW_WINDOWS_TIMEZONE:
        return
    detected = f" Сейчас Windows сообщает: «{timezone_id}»." if timezone_id else ""
    raise RuntimeError(
        "Планировщик Windows запускает задачи по локальному времени компьютера. "
        "Установите часовой пояс Windows на «(UTC+03:00) Москва, Санкт-Петербург» "
        f"(идентификатор Russian Standard Time) и повторите schedule install.{detected}"
    )


def _run_powershell(script: str, timeout: int = 30) -> str:
    _ensure_windows()
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-STA", "-WindowStyle", "Hidden",
             "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"Не удалось выполнить PowerShell: {exc}") from exc
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"Операция с Планировщиком Windows завершилась ошибкой: {detail or result.returncode}")
    return result.stdout.strip()


def install_schedule(python_executable: str) -> list[dict[str, Any]]:
    """Install/update the app-owned hourly collection and daily digest tasks."""
    _ensure_windows()
    _ensure_moscow_timezone()
    python_path = Path(python_executable).resolve()
    if not python_path.is_file():
        raise ValueError(f"Не найден Python для запланированных запусков: {python_path}")
    app_root = str(APP_ROOT.resolve())
    user_name = os.environ.get("USERNAME") or getpass.getuser()
    domain = os.environ.get("USERDOMAIN", "")
    user_id = f"{domain}\\{user_name}" if domain else user_name

    task_literals = ",\n    ".join(
        "@{{ Name={}; Frequency={}; Hour={}; IntervalMinutes={}; Arguments={} }}".format(
            _ps_quote(task["name"]), _ps_quote(task["frequency"]),
            task.get("hour", 0), task.get("interval_minutes", 0),
            _ps_quote(task["arguments"])
        ) for task in TASKS
    )
    script = f"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$taskSpecs = @(
    {task_literals}
)
$taskDescription = {_ps_quote(TASK_DESCRIPTION)}
$triggerEndBoundary = {_ps_quote(TRIGGER_END_BOUNDARY)}
$retiredTaskNames = @({','.join(_ps_quote(name) for name in RETIRED_TASKS)})
$pythonPath = {_ps_quote(str(python_path))}
$workingDirectory = {_ps_quote(app_root)}
$userId = {_ps_quote(user_id)}
foreach ($spec in $taskSpecs) {{
    $existing = Get-ScheduledTask -TaskName $spec.Name -ErrorAction SilentlyContinue
    if ($existing -and $existing.Description -ne $taskDescription) {{
        throw "Задача '$($spec.Name)' уже существует и не принадлежит приложению; она не изменена."
    }}
}}
foreach ($name in $retiredTaskNames) {{
    $existing = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if (-not $existing) {{ continue }}
    if ($existing.Description -ne $taskDescription) {{
        throw "Задача '$name' не принадлежит приложению; она не изменена."
    }}
    Unregister-ScheduledTask -TaskName $name -Confirm:$false
}}
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 45) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -WakeToRun -StartWhenAvailable
$principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType Interactive -RunLevel Limited
foreach ($spec in $taskSpecs) {{
    $action = New-ScheduledTaskAction -Execute $pythonPath -Argument $spec.Arguments -WorkingDirectory $workingDirectory
    if ($spec.Frequency -eq 'hourly') {{
        # Windows rejects very long repetition durations in Task Scheduler XML.
        # Use 24 daily triggers instead: one at each hour, recurring every day.
        $triggers = @(
            for ($hour = 0; $hour -lt 24; $hour++) {{
                New-ScheduledTaskTrigger -Daily -At ([datetime]::Today.AddHours($hour))
            }}
        )
    }} else {{
        $triggers = @(New-ScheduledTaskTrigger -Daily -At ([datetime]::Today.AddHours($spec.Hour)))
    }}
    foreach ($trigger in $triggers) {{ $trigger.EndBoundary = $triggerEndBoundary }}
    Register-ScheduledTask -TaskName $spec.Name -Action $action -Trigger $triggers -Settings $settings `
        -Principal $principal -Description $taskDescription -Force | Out-Null
}}
'Расписание установлено.'
"""
    _run_powershell(script)
    return schedule_status()


def schedule_status() -> list[dict[str, Any]]:
    _ensure_windows()
    task_specs = ",\n    ".join(
        "@{{ Name={}; Key={}; Arguments={}; ExpectedTriggers={} }}".format(
            _ps_quote(task["name"]),
            _ps_quote("hourly" if task["frequency"] == "hourly" else str(task["hour"])),
            _ps_quote(task["arguments"]),
            24 if task["frequency"] == "hourly" else 1,
        )
        for task in TASKS
    )
    script = f"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$taskSpecs = @(
    {task_specs}
)
$expectedPython = {_ps_quote(str(Path(sys.executable).resolve()))}
$expectedWorkingDirectory = {_ps_quote(str(APP_ROOT.resolve()))}
$expectedEndBoundary = {_ps_quote(TRIGGER_END_BOUNDARY)}
$items = foreach ($spec in $taskSpecs) {{
    $name = $spec.Name
    $task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if (-not $task) {{
        [pscustomobject]@{{ key=$spec.Key; registered=$false }}
    }} else {{
        $info = Get-ScheduledTaskInfo -InputObject $task
        $action = $task.Actions | Select-Object -First 1
        $triggers = @($task.Triggers)
        $matches = ($action.Execute -ieq $expectedPython) -and `
            ($action.Arguments -eq $spec.Arguments) -and `
            ($action.WorkingDirectory -ieq $expectedWorkingDirectory) -and `
            ([bool]$task.Settings.WakeToRun) -and `
            ([bool]$task.Settings.StartWhenAvailable) -and `
            ([string]$triggers[0].EndBoundary).StartsWith($expectedEndBoundary) -and `
            ($triggers.Count -eq $spec.ExpectedTriggers)
        [pscustomobject]@{{
            key=$spec.Key; registered=$true; state=[string]$task.State
            nextRunTime=$info.NextRunTime.ToString('o')
            lastRunTime=$info.LastRunTime.ToString('o')
            lastTaskResult=$info.LastTaskResult
            wakeToRun=[bool]$task.Settings.WakeToRun
            startWhenAvailable=[bool]$task.Settings.StartWhenAvailable
            triggerEndBoundary=[string]$triggers[0].EndBoundary
            triggerCount=$triggers.Count
            actionMatches=[bool]$matches
        }}
    }}
}}
ConvertTo-Json -InputObject @($items) -Compress -Depth 3
"""
    output = _run_powershell(script)
    if not output:
        raise RuntimeError("PowerShell не вернул состояние запланированных задач")
    try:
        parsed = json.loads(output)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Не удалось разобрать ответ Планировщика Windows: {output}") from exc
    return parsed if isinstance(parsed, list) else [parsed]


def remove_schedule() -> list[str]:
    """Remove only tasks carrying this application's ownership description."""
    _ensure_windows()
    task_names = ",".join(
        _ps_quote(name) for name in
        [task["name"] for task in TASKS] + list(RETIRED_TASKS)
    )
    script = f"""
$ErrorActionPreference = 'Stop'
$taskNames = @({task_names})
$taskDescription = {_ps_quote(TASK_DESCRIPTION)}
$removed = @()
foreach ($name in $taskNames) {{
    $task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if (-not $task) {{ continue }}
    if ($task.Description -ne $taskDescription) {{
        throw "Задача '$name' не принадлежит приложению; она не удалена."
    }}
    Unregister-ScheduledTask -TaskName $name -Confirm:$false
    $removed += $name
}}
ConvertTo-Json -InputObject @($removed) -Compress
"""
    output = _run_powershell(script)
    if not output:
        return []
    parsed = json.loads(output)
    if parsed is None:
        return []
    return parsed if isinstance(parsed, list) else [parsed]


def notify_editor(title: str, message: str) -> tuple[bool, str]:
    """Show a short Windows notification balloon in the logged-in user's session."""
    if os.name != "nt":
        return False, "уведомление поддерживается только в Windows"
    payload = base64.b64encode(json.dumps(
        {"title": title[:63], "message": message[:240]}, ensure_ascii=False
    ).encode("utf-16le")).decode("ascii")
    script = f"""
$ErrorActionPreference = 'Stop'
$payload = [System.Text.Encoding]::Unicode.GetString([System.Convert]::FromBase64String('{payload}')) | ConvertFrom-Json
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$icon = New-Object System.Windows.Forms.NotifyIcon
$icon.Icon = [System.Drawing.SystemIcons]::Warning
$icon.BalloonTipTitle = [string]$payload.title
$icon.BalloonTipText = [string]$payload.message
$icon.BalloonTipIcon = [System.Windows.Forms.ToolTipIcon]::Warning
$icon.Visible = $true
$icon.ShowBalloonTip(8000)
Start-Sleep -Seconds 8
$icon.Dispose()
"""
    try:
        _run_powershell(script, timeout=20)
        return True, "команда отображения уведомления Windows выполнена"
    except RuntimeError as exc:
        return False, str(exc)
