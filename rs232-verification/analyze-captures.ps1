param([string]$CaptureRoot = (Join-Path $PSScriptRoot 'captures'))
$ErrorActionPreference = 'Stop'
$allFrames = [System.Collections.Generic.List[object]]::new()
$summaries = [System.Collections.Generic.List[object]]::new()
$culture = [Globalization.CultureInfo]::InvariantCulture

function ConvertTo-UtcIso($Value) {
    if ($Value -is [DateTime]) { return $Value.ToUniversalTime().ToString('o') }
    return ([DateTimeOffset]::Parse([string]$Value)).ToUniversalTime().ToString('o')
}

function Get-Percentile($Values, [double]$P) {
    if ($Values.Count -eq 0) { return $null }
    $sorted = @($Values | Sort-Object)
    return [Math]::Round($sorted[[Math]::Floor(($sorted.Count - 1) * $P)], 3)
}

foreach ($file in (Get-ChildItem -LiteralPath $CaptureRoot -Filter '*.json' -Recurse | Sort-Object FullName)) {
    $data = Get-Content -LiteralPath $file.FullName -Raw | ConvertFrom-Json
    if (-not $data.PSObject.Properties['chunks']) { continue }
    $frames = [System.Collections.Generic.List[object]]::new()
    $pending = [System.Collections.Generic.List[byte]]::new()
    $nulOffsets = [System.Collections.Generic.List[int]]::new()
    $offset = 0
    $frameStart = 0
    foreach ($chunk in $data.chunks) {
        if ($chunk.direction -ne 'RX') { continue }
        [byte[]]$bytes = @($chunk.hex -split ' ' | ForEach-Object { [Convert]::ToByte($_,16) })
        foreach ($b in $bytes) {
            if ($b -eq 0) { $nulOffsets.Add($offset) }
            $pending.Add($b)
            $offset++
            if ($pending.Count -lt 2 -or $pending[$pending.Count-2] -ne 13 -or $pending[$pending.Count-1] -ne 10) { continue }
            $frameBytes = $pending.ToArray()
            $text = [Text.Encoding]::ASCII.GetString($frameBytes, 0, $frameBytes.Length-2)
            # Treat bytes before the first recognizable ST/US/OL header as synchronization residue,
            # but preserve them verbatim in raw files and per-frame diagnostics.
            $m = [regex]::Match($text, '(?<st>[A-Z]{2}),(?<mode>[A-Z]{2}),(?<sign>[+-])(?<number> *\d+\.\d+)(?<unitfield> *[A-Za-z]+)$')
            $prefix = if ($m.Success) { $text.Substring(0,$m.Index) } else { '' }
            $record = [pscustomobject][ordered]@{
                capture=$data.label; file=$file.Directory.Name; index=$frames.Count+1;
                elapsed_ms=$chunk.elapsed_ms; utc=(ConvertTo-UtcIso $chunk.utc); raw_offset=$frameStart;
                raw_bytes=$frameBytes.Length; matched=$m.Success; prefix_hex=([BitConverter]::ToString([Text.Encoding]::ASCII.GetBytes($prefix)).Replace('-',' '));
                frame_bytes=$(if($m.Success){$m.Length+2}else{$frameBytes.Length});
                status=$m.Groups['st'].Value; mode=$m.Groups['mode'].Value; sign=$m.Groups['sign'].Value;
                number_field=$m.Groups['number'].Value; unit_field=$m.Groups['unitfield'].Value;
                value=$(if($m.Success){[double]::Parse($m.Groups['sign'].Value+$m.Groups['number'].Value.Trim(),$culture)}else{$null});
                unit=$m.Groups['unitfield'].Value.Trim();
                ascii=$(if($m.Success){$m.Value}else{$text});
                hex=([BitConverter]::ToString($frameBytes).Replace('-',' '))
            }
            $frames.Add($record)
            $allFrames.Add($record)
            $pending.Clear()
            $frameStart=$offset
        }
    }
    $valid = @($frames | Where-Object matched)
    $intervals = @(for($i=1;$i -lt $valid.Count;$i++) { $valid[$i].elapsed_ms-$valid[$i-1].elapsed_ms })
    $summary = [pscustomobject][ordered]@{
        label=$data.label; folder=$file.Directory.Name; start_utc=(ConvertTo-UtcIso $data.start_utc); baud=$data.baud;
        duration_s=$data.duration_observed_s; error=$data.error; rx_bytes=$data.rx_bytes;
        tx=@($data.chunks | Where-Object direction -eq 'TX');
        complete_crlf_records=$frames.Count; recognizable_frames=$valid.Count;
        unrecognized_records=@($frames | Where-Object {-not $_.matched}).Count;
        leading_residue=@($frames | Where-Object prefix_hex | Select-Object index,prefix_hex);
        trailing_hex=([BitConverter]::ToString($pending.ToArray()).Replace('-',' '));
        nul_offsets=@($nulOffsets.ToArray());
        frame_lengths=@($valid | Group-Object frame_bytes | Select-Object Name,Count);
        statuses=@($valid | Group-Object status | Select-Object Name,Count);
        modes=@($valid | Group-Object mode | Select-Object Name,Count);
        signs=@($valid | Group-Object sign | Select-Object Name,Count);
        units=@($valid | Group-Object unit | Select-Object Name,Count);
        min_value=$(if($valid.Count){($valid.value | Measure-Object -Minimum).Minimum}else{$null});
        max_value=$(if($valid.Count){($valid.value | Measure-Object -Maximum).Maximum}else{$null});
        rate_hz=$(if($valid.Count -gt 1){[Math]::Round(1000*($valid.Count-1)/($valid[-1].elapsed_ms-$valid[0].elapsed_ms),4)}else{$null});
        interval_ms=[ordered]@{min=Get-Percentile $intervals 0; p05=Get-Percentile $intervals 0.05; median=Get-Percentile $intervals 0.5; p95=Get-Percentile $intervals 0.95; max=Get-Percentile $intervals 1};
        examples=@($valid | Group-Object status,mode,sign,unit | ForEach-Object { $_.Group[0] | Select-Object elapsed_ms,ascii,hex,frame_bytes });
        value_counts=@($valid | Group-Object value | Sort-Object Count -Descending | Select-Object -First 12 Name,Count);
        transitions=@(for($i=0;$i -lt $valid.Count;$i++) { if($i -eq 0 -or $valid[$i].status -ne $valid[$i-1].status -or $valid[$i].mode -ne $valid[$i-1].mode -or [Math]::Abs($valid[$i].value-$valid[$i-1].value) -ge 0.5) { $valid[$i] | Select-Object elapsed_ms,status,mode,value,unit,ascii } })
    }
    $summaries.Add($summary)
}
$summaries.ToArray() | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath (Join-Path $PSScriptRoot 'analysis.json') -Encoding utf8
$allFrames.ToArray() | Export-Csv -LiteralPath (Join-Path $PSScriptRoot 'frames.csv') -NoTypeInformation -Encoding utf8
$summaries.ToArray() | ConvertTo-Json -Depth 10
