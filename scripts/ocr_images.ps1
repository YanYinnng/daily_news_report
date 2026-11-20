param([Parameter(Mandatory=$true)][string]$InputManifest,
      [Parameter(Mandatory=$true)][string]$OutputPath)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Storage.StorageFile,Windows.Storage,ContentType=WindowsRuntime]
$null = [Windows.Storage.Streams.IRandomAccessStream,Windows.Storage.Streams,ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder,Windows.Graphics.Imaging,ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.SoftwareBitmap,Windows.Graphics.Imaging,ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapTransform,Windows.Graphics.Imaging,ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapBounds,Windows.Graphics.Imaging,ContentType=WindowsRuntime]
$null = [Windows.Media.Ocr.OcrEngine,Windows.Foundation,ContentType=WindowsRuntime]
$null = [Windows.Globalization.Language,Windows.Globalization,ContentType=WindowsRuntime]
$asTask = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.IsGenericMethod -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
} | Select-Object -First 1
function Await-Result($Operation, [Type]$ResultType) {
    $task = $asTask.MakeGenericMethod($ResultType).Invoke($null, @($Operation))
    $task.Wait()
    return $task.Result
}
$language = New-Object Windows.Globalization.Language('zh-Hans-CN')
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($language)
if (-not $engine) { throw 'Chinese OCR language is unavailable' }
$inputPaths = Get-Content -LiteralPath $InputManifest -Raw -Encoding UTF8 | ConvertFrom-Json
$results = @()
foreach ($imagePath in $inputPaths) {
    $stream = $null; $bitmap = $null
    try {
        $resolved = (Resolve-Path -LiteralPath $imagePath).Path
        $file = Await-Result ([Windows.Storage.StorageFile]::GetFileFromPathAsync($resolved)) ([Windows.Storage.StorageFile])
        $stream = Await-Result ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
        $decoder = Await-Result ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
        # Decode bounded strips instead of allocating an entire tall poster.
        # Native API: BitmapDecoder.GetSoftwareBitmapAsync with BitmapTransform.Bounds.
        $limit = [int][Windows.Media.Ocr.OcrEngine]::MaxImageDimension
        if ($decoder.PixelWidth -le $limit -and $decoder.PixelHeight -le $limit) {
            $bitmap = Await-Result ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
            $ocr = Await-Result ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
            $results += @{ path=$resolved; status='ok'; text=(($ocr.Lines | ForEach-Object { $_.Text }) -join "`n"); strips=1; engine_version=3 }
            continue
        }
        $width = [int][Math]::Min($decoder.PixelWidth, $limit)
        $height = [int][Math]::Ceiling($decoder.PixelHeight * ($width / [double]$decoder.PixelWidth))
        $overlap = 96
        $step = $limit - $overlap
        if ([Math]::Ceiling($height / [double]$step) -gt 64) { throw 'Image exceeds the bounded OCR strip count' }
        $lines = New-Object 'System.Collections.Generic.List[string]'
        $tiles = 0
        for ($top = 0; $top -lt $height; $top += $step) {
            $tileHeight = [int][Math]::Min($limit, $height - $top)
            $last = $top + $tileHeight -ge $height
            $transform = New-Object Windows.Graphics.Imaging.BitmapTransform
            $transform.ScaledWidth = [uint32]$width
            $transform.ScaledHeight = [uint32]$height
            $bounds = New-Object Windows.Graphics.Imaging.BitmapBounds
            $bounds.X = 0; $bounds.Y = [uint32]$top
            $bounds.Width = [uint32]$width; $bounds.Height = [uint32]$tileHeight
            $transform.Bounds = $bounds
            $bitmap = Await-Result ($decoder.GetSoftwareBitmapAsync(
                [Windows.Graphics.Imaging.BitmapPixelFormat]::Bgra8,
                [Windows.Graphics.Imaging.BitmapAlphaMode]::Premultiplied, $transform,
                [Windows.Graphics.Imaging.ExifOrientationMode]::IgnoreExifOrientation,
                [Windows.Graphics.Imaging.ColorManagementMode]::DoNotColorManage)) ([Windows.Graphics.Imaging.SoftwareBitmap])
            $ocr = Await-Result ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
            $tileLines = @($ocr.Lines | ForEach-Object { $_.Text } | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
            $skip = 0
            # Keep every strip; remove only matching overlap lines.
            for ($candidate = [Math]::Min(64, [Math]::Min($lines.Count, $tileLines.Count)); $candidate -gt 0; $candidate--) {
                $same = $true
                for ($index = 0; $index -lt $candidate; $index++) {
                    if ($lines[$lines.Count-$candidate+$index].Trim() -ne $tileLines[$index].Trim()) { $same = $false; break }
                }
                if ($same) { $skip = $candidate; break }
            }
            for ($index = $skip; $index -lt $tileLines.Count; $index++) { $lines.Add($tileLines[$index]) }
            $bitmap.Dispose(); $bitmap = $null
            $tiles++
            if ($last) { break }
        }
        $results += @{ path=$resolved; status='ok'; text=($lines -join "`n"); strips=$tiles; engine_version=3 }
    } catch {
        $results += @{ path=$imagePath; status='error'; message=$_.Exception.Message; where=$_.ScriptStackTrace; engine_version=3 }
    } finally {
        if ($bitmap) { $bitmap.Dispose() }
        if ($stream) { $stream.Dispose() }
    }
}
$json = ConvertTo-Json -InputObject @($results) -Depth 4
[System.IO.File]::WriteAllText($OutputPath, $json, (New-Object System.Text.UTF8Encoding($false)))
