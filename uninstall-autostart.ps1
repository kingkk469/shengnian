# Remove voice-journal scheduled tasks.
foreach ($n in 'voice-journal-recorder', 'voice-journal-transcriber', 'voice-journal-daily-summary') {
    try {
        Unregister-ScheduledTask -TaskName $n -Confirm:$false -ErrorAction Stop
        Write-Host "Removed: $n"
    } catch {
        Write-Host "Skipped (not found): $n"
    }
}
