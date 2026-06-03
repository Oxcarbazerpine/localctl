# One-time setup: register the `localctl-autostart` Windows event source so
# autostart.bat can write structured failure events to Application log without
# admin privileges on subsequent runs.
#
# MUST be run as Administrator (registering a new event source is a privileged
# operation; once registered any user can write to it).
#
# Usage (elevated PowerShell):
#   .\register-eventsource.ps1

if (-not ([Security.Principal.WindowsPrincipal] `
        [Security.Principal.WindowsIdentity]::GetCurrent()
    ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Error "This script must be run as Administrator."
    exit 1
}

$source = 'localctl-autostart'
if ([System.Diagnostics.EventLog]::SourceExists($source)) {
    Write-Host "event source already registered: $source"
} else {
    [System.Diagnostics.EventLog]::CreateEventSource($source, 'Application')
    Write-Host "registered event source: $source (log: Application)"
}

# Smoke-test by writing one INFORMATION entry.
Write-EventLog -LogName Application -Source $source -EntryType Information `
    -EventId 1 -Message 'localctl-autostart event source registered.'
Write-Host "test event written. View it: Get-WinEvent -ProviderName $source -MaxEvents 5"
