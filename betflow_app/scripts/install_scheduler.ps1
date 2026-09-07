# Instala a Tarefa Agendada do Windows que roda a coleta de odds
# automaticamente a cada N horas (padrao: 2h), sem voce precisar fazer nada.
#
# Uso (rode UMA vez, no PowerShell, dentro da pasta do projeto):
#     powershell -ExecutionPolicy Bypass -File scripts\install_scheduler.ps1
#     powershell -ExecutionPolicy Bypass -File scripts\install_scheduler.ps1 -IntervalHours 3
#
# Para remover depois:
#     Unregister-ScheduledTask -TaskName "BetflowColetaOdds" -Confirm:$false

param(
    [int]$IntervalHours = 2,
    [string]$TaskName = "BetflowColetaOdds"
)

$ErrorActionPreference = "Stop"

# raiz do projeto = pasta pai deste script
$ScriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectDir = Split-Path -Parent $ScriptDir
$Python     = Join-Path $ProjectDir ".venv\Scripts\pythonw.exe"
$Collector  = Join-Path $ProjectDir "scripts\collect_odds.py"

if (-not (Test-Path $Python)) {
    Write-Error "Python do venv nao encontrado em: $Python"
    exit 1
}

Write-Host "Projeto:  $ProjectDir"
Write-Host "Python:   $Python"
Write-Host "Intervalo: a cada $IntervalHours hora(s)"

# acao: roda o coletor (pythonw = sem janela de console)
$Action = New-ScheduledTaskAction -Execute $Python `
    -Argument "`"$Collector`"" -WorkingDirectory $ProjectDir

# gatilho: comeca agora e repete a cada N horas, indefinidamente
$Trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Hours $IntervalHours)

# so roda quando houver rede; nao acorda o PC; roda no seu usuario
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries `
    -RunOnlyIfNetworkAvailable

# remove versao antiga (se existir) e registra a nova
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Tarefa anterior removida."
}

Register-ScheduledTask -TaskName $TaskName -Action $Action `
    -Trigger $Trigger -Settings $Settings `
    -Description "Betflow: coleta automatica de odds (snapshots p/ CLV)." | Out-Null

Write-Host ""
Write-Host "OK! Tarefa '$TaskName' registrada."
Write-Host "Ela roda a cada $IntervalHours h enquanto o PC estiver ligado."
Write-Host "Ver no Agendador de Tarefas do Windows (taskschd.msc)."
Write-Host "Rodar agora manualmente: Start-ScheduledTask -TaskName $TaskName"
