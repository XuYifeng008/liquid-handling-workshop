$ErrorActionPreference = 'Stop'
$checks = [System.Collections.Generic.List[object]]::new()
$jsonFiles = @(Get-ChildItem -LiteralPath (Join-Path $PSScriptRoot 'captures') -Filter '*.json' -Recurse)
foreach ($file in $jsonFiles) {
    $data = Get-Content -Raw -LiteralPath $file.FullName | ConvertFrom-Json
    $binaryPath = [IO.Path]::ChangeExtension($file.FullName, '.bin')
    $rawBytes = [IO.File]::ReadAllBytes($binaryPath)
    $reconstructed = [Collections.Generic.List[byte]]::new()
    foreach ($chunk in $data.chunks) {
        if ($chunk.direction -ne 'RX') { continue }
        [byte[]]$part = @($chunk.hex -split ' ' | ForEach-Object { [Convert]::ToByte($_,16) })
        if ($part.Length -ne $chunk.length) { throw "Chunk length mismatch: $($file.FullName)" }
        $reconstructed.AddRange($part)
    }
    if ($rawBytes.Length -ne $reconstructed.Count -or $rawBytes.Length -ne $data.rx_bytes) { throw "Length mismatch: $binaryPath" }
    for ($i=0; $i -lt $rawBytes.Length; $i++) {
        if ($rawBytes[$i] -ne $reconstructed[$i]) { throw "Byte mismatch at $i in $binaryPath" }
    }
    $tx = @($data.chunks | Where-Object direction -eq 'TX')
    if ($tx.Count -gt 0 -or $data.tx_read_request_sent) { throw "Unexpected transmit recorded: $($file.FullName)" }
    $checks.Add([pscustomobject]@{capture=$data.label; raw_bytes=$rawBytes.Length; json_matches_binary=$true; no_tx=$true; open_succeeded=($null -eq $data.error)})
}

$summaries = @(Get-Content -Raw -LiteralPath (Join-Path $PSScriptRoot 'analysis.json') | ConvertFrom-Json)
$frames = @(Import-Csv -LiteralPath (Join-Path $PSScriptRoot 'frames.csv'))
$successful = @($summaries | Where-Object {$null -eq $_.error})
$expectedFrameCount = ($successful.recognizable_frames | Measure-Object -Sum).Sum
if ($frames.Count -ne $expectedFrameCount) { throw 'CSV frame count mismatch' }
foreach ($frame in $frames) {
    if ($frame.matched -ne 'True') { throw "Unrecognized frame in $($frame.file)" }
    $recordBytes = @($frame.hex -split ' ' | ForEach-Object { [Convert]::ToByte($_,16) })
    if ($recordBytes.Length -ne [int]$frame.raw_bytes) { throw 'Raw frame length mismatch' }
    if ($recordBytes[-2] -ne 13 -or $recordBytes[-1] -ne 10) { throw 'Missing CR LF' }
    $prefixLength = if ($frame.prefix_hex) { @($frame.prefix_hex -split ' ').Count } else { 0 }
    $visible = [Text.Encoding]::ASCII.GetString([byte[]]$recordBytes, $prefixLength, $recordBytes.Length-$prefixLength-2)
    if ($visible -cne $frame.ascii) { throw 'CSV text differs from raw bytes' }
    if ($visible.Length+2 -ne [int]$frame.frame_bytes) { throw 'Frame byte count mismatch' }
    if ($visible -cne "$($frame.status),$($frame.mode),$($frame.sign)$($frame.number_field)$($frame.unit_field)") { throw 'Parsed fields differ from original ASCII' }
    $value = [double]::Parse($frame.sign+$frame.number_field.Trim(), [Globalization.CultureInfo]::InvariantCulture)
    if ($value -ne [double]::Parse($frame.value, [Globalization.CultureInfo]::InvariantCulture)) { throw 'Parsed signed value mismatch' }
}

$manifest = @(Get-ChildItem -LiteralPath (Join-Path $PSScriptRoot 'captures') -File -Recurse | Sort-Object FullName | ForEach-Object {
    [pscustomobject]@{relative_path=[IO.Path]::GetRelativePath($PSScriptRoot,$_.FullName); bytes=$_.Length; sha256=(Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash}
})
$manifest | Export-Csv -LiteralPath (Join-Path $PSScriptRoot 'SHA256.csv') -NoTypeInformation -Encoding utf8
$result = [ordered]@{
    verified_utc=[DateTimeOffset]::UtcNow.ToString('o'); captures=@($checks.ToArray());
    successful_captures=$successful.Count; validated_csv_frames=$frames.Count;
    source_docx_sha256=(Get-FileHash -LiteralPath (Join-Path (Split-Path $PSScriptRoot -Parent) '232通讯协议说明.docx') -Algorithm SHA256).Hash;
    raw_and_json_byte_identity='PASS'; csv_raw_fields_and_signed_value='PASS'; all_captures_no_tx='PASS'
}
$result | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $PSScriptRoot 'verification.json') -Encoding utf8
$result | ConvertTo-Json -Depth 6
