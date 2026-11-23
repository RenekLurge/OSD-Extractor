# M1 侦察工具：Windows 内置 OCR 扫图，输出每个词 文本/坐标/字号
# 用法: powershell -NoProfile -ExecutionPolicy Bypass -File win_ocr_recon.ps1 <图片路径>
param([Parameter(Mandatory=$true)][string]$ImagePath)
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$null = [Windows.Media.Ocr.OcrEngine,Windows.Foundation,ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.SoftwareBitmap,Windows.Foundation,ContentType=WindowsRuntime]
$null = [Windows.Storage.StorageFile,Windows.Storage,ContentType=WindowsRuntime]
Add-Type -AssemblyName System.Runtime.WindowsRuntime

$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]

Function Await($WinRtTask, $ResultType) {
    $asTask = $asTaskGeneric.MakeGenericMethod($ResultType)
    $netTask = $asTask.Invoke($null, @($WinRtTask))
    $netTask.Wait(-1) | Out-Null
    $netTask.Result
}

$file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($ImagePath)) ([Windows.Storage.StorageFile])
$stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
$decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
$bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])

$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
if ($null -eq $engine) {
    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage([Windows.Globalization.Language]::new('en-US'))
}
if ($null -eq $engine) { Write-Output "NO_OCR_ENGINE"; exit 1 }
Write-Output ("ENGINE: " + $engine.RecognizerLanguage.LanguageTag)

$result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
Write-Output ("LINES: " + $result.Lines.Size)
for ($i = 0; $i -lt $result.Lines.Size; $i++) {
    $line = $result.Lines.GetAt($i)
    for ($j = 0; $j -lt $line.Words.Size; $j++) {
        $w = $line.Words.GetAt($j)
        $r = $w.BoundingRect
        Write-Output ("[{0},{1} +{2}x{3}] {4}" -f [int]$r.X, [int]$r.Y, [int]$r.Width, [int]$r.Height, $w.Text)
    }
    Write-Output "---"
}
