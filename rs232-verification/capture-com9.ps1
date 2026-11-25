param(
    [int[]]$BaudRates = @(9600, 4800, 2400, 1200),
    [double]$DurationSeconds = 8,
    [string]$Label = 'passive',
    [string]$OutputDirectory = '',
    [switch]$ReadRequest
)

$ErrorActionPreference = 'Stop'
if (-not $OutputDirectory) {
    $stamp = [TimeZoneInfo]::ConvertTimeBySystemTimeZoneId([DateTimeOffset]::UtcNow, 'China Standard Time').ToString('yyyyMMdd-HHmmss')
    $OutputDirectory = Join-Path $PSScriptRoot "captures/$stamp-$Label"
}
[void][System.IO.Directory]::CreateDirectory($OutputDirectory)
$OutputDirectory = [System.IO.Path]::GetFullPath($OutputDirectory)

function ConvertTo-VisibleAscii([byte[]]$Data) {
    return (($Data | ForEach-Object {
        if ($_ -ge 32 -and $_ -le 126) { [string][char]$_ }
        else { '<{0:X2}>' -f $_ }
    }) -join '')
}

foreach ($baud in $BaudRates) {
    $port = [System.IO.Ports.SerialPort]::new('COM9', $baud, [System.IO.Ports.Parity]::None, 8, [System.IO.Ports.StopBits]::One)
    $port.Handshake = [System.IO.Ports.Handshake]::None
    $port.DtrEnable = $false
    $port.RtsEnable = $false
    $port.ReadTimeout = 200
    $port.WriteTimeout = 1000
    $chunks = [System.Collections.Generic.List[object]]::new()
    $raw = [System.IO.MemoryStream]::new()
    $start = [DateTimeOffset]::UtcNow
    $watch = [System.Diagnostics.Stopwatch]::new()
    $problem = $null
    $sent = $false
    try {
        $port.Open()
        $start = [DateTimeOffset]::UtcNow
        $watch.Start()
        Write-Output "OPEN COM9 $baud 8N1; capture $DurationSeconds s; read-request=$ReadRequest"
        $buffer = [byte[]]::new(8192)
        while ($watch.Elapsed.TotalSeconds -lt $DurationSeconds) {
            if ($ReadRequest -and -not $sent -and $watch.Elapsed.TotalSeconds -ge 2) {
                # Only the document's read-weight request: one ASCII R, no terminator.
                $tx = [byte[]]@(0x52)
                $port.Write($tx, 0, $tx.Length)
                $chunks.Add([pscustomobject]@{direction='TX'; elapsed_ms=[Math]::Round($watch.Elapsed.TotalMilliseconds,3); utc=[DateTimeOffset]::UtcNow.ToString('o'); hex='52'; ascii='R'; length=1})
                $sent = $true
            }
            $available = $port.BytesToRead
            if ($available -gt 0) {
                $count = $port.Read($buffer, 0, [Math]::Min($available, $buffer.Length))
                $part = [byte[]]::new($count)
                [Array]::Copy($buffer, $part, $count)
                $raw.Write($part, 0, $count)
                $chunks.Add([pscustomobject]@{direction='RX'; elapsed_ms=[Math]::Round($watch.Elapsed.TotalMilliseconds,3); utc=[DateTimeOffset]::UtcNow.ToString('o'); hex=[BitConverter]::ToString($part).Replace('-', ' '); ascii=(ConvertTo-VisibleAscii $part); length=$count})
            } else { Start-Sleep -Milliseconds 2 }
        }
    } catch {
        $problem = $_.Exception.ToString()
        Write-Output "ERROR: $problem"
    } finally {
        $watch.Stop()
        if ($port.IsOpen) { $port.Close() }
        $port.Dispose()
        $allBytes = $raw.ToArray()
        $raw.Dispose()
        $base = Join-Path $OutputDirectory "COM9-$baud"
        [System.IO.File]::WriteAllBytes("$base.bin", $allBytes)
        $metadata = [ordered]@{
            port='COM9'; baud=$baud; data_bits=8; parity='None'; stop_bits=1;
            handshake='None'; dtr=$false; rts=$false; label=$Label;
            start_utc=$start.ToString('o'); duration_requested_s=$DurationSeconds;
            duration_observed_s=[Math]::Round($watch.Elapsed.TotalSeconds,3);
            timing_note='RX timestamps are host read times, not per-byte wire times. Read chunks are not protocol frames.';
            tx_read_request_sent=$sent; rx_bytes=$allBytes.Length; error=$problem;
            chunks=@($chunks.ToArray())
        }
        $metadata | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath "$base.json" -Encoding utf8
        $visible = ConvertTo-VisibleAscii $allBytes
        [System.IO.File]::WriteAllText("$base.ascii.txt", $visible, [System.Text.UTF8Encoding]::new($false))
        $printable = @($allBytes | Where-Object { ($_ -ge 32 -and $_ -le 126) -or $_ -eq 10 -or $_ -eq 13 }).Count
        $ratio = if ($allBytes.Length) { [Math]::Round(100 * $printable / $allBytes.Length, 2) } else { 0 }
        Write-Output "RESULT baud=$baud bytes=$($allBytes.Length) text_or_CRLF_pct=$ratio chunks=$($chunks.Count) output=$base"
        Write-Output $visible.Substring(0, [Math]::Min($visible.Length, 450))
    }
    if ($problem) { break }
}
