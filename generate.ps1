<#
.SYNOPSIS
    Builds docs/lines/NNN.json: lines and headsigns per Prague platform, from the
    PID GTFS feed. Published by GitHub Pages and downloaded by the PID watch app.

.DESCRIPTION
    Same rules as tools/generate-stop-list.ps1 in the PID app (Prague = fare zone P,
    trips that end at a stop are skipped), but 128 files: a stop is in file
    (sum of its name's character codes) mod 128. Each file:
      { "Florenc": [ ["U689Z2P", "B", ["3", "6", ...], ["Kobylisy", ...]], ... ], ... }
    Non-ASCII characters are written as \uXXXX. Also writes docs/meta.json.
    Runs with PowerShell 7 (GitHub Actions, Linux) and Windows PowerShell 5.1.

.EXAMPLE
    pwsh ./generate.ps1
#>

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$Buckets = 128
$url     = "https://data.pid.cz/PID_GTFS.zip"
$outDir  = Join-Path (Join-Path $PSScriptRoot "docs") "lines"
$tmp     = Join-Path ([IO.Path]::GetTempPath()) "pid-gtfs"
New-Item -ItemType Directory -Force -Path $tmp | Out-Null
New-Item -ItemType Directory -Force -Path $outDir | Out-Null
$zip     = Join-Path $tmp "PID_GTFS.zip"

Write-Host "Downloading $url ..."
Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing
Write-Host ("Downloaded {0:N0} bytes" -f (Get-Item $zip).Length)

Add-Type -AssemblyName System.IO.Compression.FileSystem
$archive = [IO.Compression.ZipFile]::OpenRead($zip)
try {
    foreach ($fileName in @("stops.txt", "routes.txt", "trips.txt", "stop_times.txt")) {
        $entry = $archive.Entries | Where-Object { $_.Name -eq $fileName } | Select-Object -First 1
        if ($null -eq $entry) { throw "$fileName not found in the GTFS zip" }
        [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, (Join-Path $tmp $fileName), $true)
    }
} finally {
    $archive.Dispose()
}

# Prague platforms (zone_id contains P), stops/platforms only.
$plat = @{}  # stop_id -> @(name, platform_code)
foreach ($r in (Import-Csv -Path (Join-Path $tmp "stops.txt") -Encoding UTF8)) {
    $lt = "$($r.location_type)".Trim()
    if ($lt -ne "" -and $lt -ne "0") { continue }
    $zones = "$($r.zone_id)".Split(",") | ForEach-Object { $_.Trim() }
    if ($zones -notcontains "P") { continue }
    $n = "$($r.stop_name)".Trim()
    if ($n -ne "") { $plat["$($r.stop_id)"] = @($n, "$($r.platform_code)".Trim()) }
}
Write-Host ("Prague platforms: {0:N0}" -f $plat.Count)

# JSON string content: escape \ and ", non-ASCII as \uXXXX.
function ConvertTo-JsonText([string]$s) {
    $sb = New-Object Text.StringBuilder
    foreach ($ch in $s.ToCharArray()) {
        $c = [int]$ch
        if ($ch -eq '\') { [void]$sb.Append('\\') }
        elseif ($ch -eq '"') { [void]$sb.Append('\"') }
        elseif ($c -lt 32 -or $c -gt 126) { [void]$sb.Append(('\u{0:x4}' -f $c)) }
        else { [void]$sb.Append($ch) }
    }
    return $sb.ToString()
}

# A CSV line with quotes (rare in stop_times.txt): split respecting "...".
function Split-CsvLine([string]$line) {
    $out = New-Object System.Collections.Generic.List[string]
    $sb = New-Object Text.StringBuilder
    $q = $false
    for ($i = 0; $i -lt $line.Length; $i++) {
        $c = $line[$i]
        if ($c -eq '"') {
            if ($q -and $i + 1 -lt $line.Length -and $line[$i + 1] -eq '"') { [void]$sb.Append('"'); $i++ }
            else { $q = -not $q }
        } elseif ($c -eq ',' -and -not $q) {
            $out.Add($sb.ToString()); [void]$sb.Clear()
        } else {
            [void]$sb.Append($c)
        }
    }
    $out.Add($sb.ToString())
    return ,$out.ToArray()
}

$routeName = @{}
foreach ($r in (Import-Csv -Path (Join-Path $tmp "routes.txt") -Encoding UTF8)) {
    $routeName["$($r.route_id)"] = "$($r.route_short_name)".Trim()
}
$tripRoute = New-Object 'System.Collections.Generic.Dictionary[string,string]'
$tripHead = New-Object 'System.Collections.Generic.Dictionary[string,string]'
foreach ($t in (Import-Csv -Path (Join-Path $tmp "trips.txt") -Encoding UTF8)) {
    $rn = $routeName["$($t.route_id)"]
    if ($null -eq $rn -or $rn -eq "") { continue }
    $tripRoute["$($t.trip_id)"] = $rn
    $tripHead["$($t.trip_id)"] = "$($t.trip_headsign)".Trim()
}
Write-Host ("Routes: {0:N0}, trips: {1:N0}" -f $routeName.Count, $tripRoute.Count)

# stop_times.txt is large: stream it line by line.
$platLines = @{}
$platHeads = @{}
$reader = New-Object IO.StreamReader((Join-Path $tmp "stop_times.txt"), [Text.Encoding]::UTF8)
try {
    $header = $reader.ReadLine().Split(",")
    $iTrip = [Array]::IndexOf($header, "trip_id")
    $iStop = [Array]::IndexOf($header, "stop_id")
    if ($iTrip -lt 0 -or $iStop -lt 0) { throw "stop_times.txt: trip_id / stop_id columns not found" }
    $count = 0
    while ($null -ne ($line = $reader.ReadLine())) {
        $count++
        if ($count % 1000000 -eq 0) { Write-Host ("  stop_times: {0:N0} rows..." -f $count) }
        $f = if ($line.IndexOf('"') -ge 0) { Split-CsvLine $line } else { $line.Split(",") }
        $sid = $f[$iStop]
        $p = $plat[$sid]
        if ($null -eq $p) { continue }
        $tid = $f[$iTrip]
        $rn = $null
        if (-not $tripRoute.TryGetValue($tid, [ref]$rn)) { continue }
        $hs = $tripHead[$tid]
        # A trip that ends at this stop does not depart from it.
        if ($hs -eq $p[0]) { continue }
        if (-not $platLines.ContainsKey($sid)) {
            $platLines[$sid] = New-Object 'System.Collections.Generic.HashSet[string]'
            $platHeads[$sid] = New-Object 'System.Collections.Generic.HashSet[string]'
        }
        [void]$platLines[$sid].Add($rn)
        if ($hs -ne "") { [void]$platHeads[$sid].Add($hs) }
    }
} finally {
    $reader.Dispose()
}
Write-Host ("stop_times rows: {0:N0}; platforms with lines: {1:N0}" -f $count, $platLines.Count)
if ($platLines.Count -lt 1000) { throw "Too few platforms with lines - the feed looks broken, nothing written." }

# Line order: letters (metro) first, then numbers in numeric order.
function Get-LineKey([string]$l) {
    if ($l -match '^\d+$') { return "1" + $l.PadLeft(5, "0") }
    return "0" + $l
}

$byName = @{}
foreach ($sid in $platLines.Keys) {
    $n = $plat[$sid][0]
    if (-not $byName.ContainsKey($n)) { $byName[$n] = New-Object System.Collections.Generic.List[string] }
    $byName[$n].Add($sid)
}
$parts = @{}
for ($b = 0; $b -lt $Buckets; $b++) { $parts[$b] = New-Object Text.StringBuilder }
foreach ($n in ($byName.Keys | Sort-Object)) {
    $sum = 0
    foreach ($ch in $n.ToCharArray()) { $sum += [int]$ch }
    $sb = $parts[$sum % $Buckets]
    if ($sb.Length -gt 0) { [void]$sb.Append(",`n") }
    [void]$sb.Append('"' + (ConvertTo-JsonText $n) + '":[')
    $sids = @($byName[$n] | Sort-Object { $plat[$_][1] }, { $_ })
    for ($k = 0; $k -lt $sids.Count; $k++) {
        $sid = $sids[$k]
        if ($k -gt 0) { [void]$sb.Append(",") }
        $ls = @($platLines[$sid] | Sort-Object { Get-LineKey $_ } | ForEach-Object { '"' + (ConvertTo-JsonText $_) + '"' })
        $hs = @($platHeads[$sid] | Sort-Object | ForEach-Object { '"' + (ConvertTo-JsonText $_) + '"' })
        [void]$sb.Append('["' + $sid + '","' + (ConvertTo-JsonText $plat[$sid][1]) + '",[' + ($ls -join ",") + '],[' + ($hs -join ",") + ']]')
    }
    [void]$sb.Append(']')
}

$utf8 = New-Object System.Text.UTF8Encoding $false
$total = 0
$largest = 0
for ($b = 0; $b -lt $Buckets; $b++) {
    $text = "{" + $parts[$b].ToString() + "}"
    [IO.File]::WriteAllText((Join-Path $outDir ("{0:D3}.json" -f $b)), $text, $utf8)
    $total += $text.Length
    if ($text.Length -gt $largest) { $largest = $text.Length }
}
$meta = '{"generated":"' + [DateTime]::UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ") + '","stops":' + $byName.Count + ',"buckets":' + $Buckets + '}'
[IO.File]::WriteAllText((Join-Path (Join-Path $PSScriptRoot "docs") "meta.json"), $meta, $utf8)

Write-Host ("Stops: {0:N0}; {1} files, {2:N0} bytes, largest {3:N0} bytes" -f $byName.Count, $Buckets, $total, $largest)
if ($platLines.ContainsKey("U689Z2P")) {
    Write-Host ("Check: Florenc B: " + ((@($platLines["U689Z2P"] | Sort-Object { Get-LineKey $_ })) -join ", "))
}
