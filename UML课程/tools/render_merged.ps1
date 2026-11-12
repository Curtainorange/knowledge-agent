# 用 Windows 自带 PDF 渲染器把每页导出为 PNG（无需第三方库）
$Pdf = "D:\知识学习智能体\UML课程\认知副驾-需求分析与系统设计-V4.0.pdf"
$OutDir = "D:\知识学习智能体\UML课程\.preview2"
$Dpi = 110

Add-Type -AssemblyName System.Runtime.WindowsRuntime
Add-Type -AssemblyName System.Drawing

$asTaskOp = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
})[0]
$asTaskAction = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncAction'
})[0]

function Await($op, $type) {
    $m = $asTaskOp.MakeGenericMethod($type)
    $t = $m.Invoke($null, @($op))
    $t.Wait(-1) | Out-Null
    $t.Result
}
function WaitAction($op) {
    $t = $asTaskAction.Invoke($null, @($op))
    $t.Wait(-1) | Out-Null
}

[Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Pdf.PdfDocument, Windows.Data.Pdf, ContentType = WindowsRuntime] | Out-Null
[Windows.Storage.Streams.IRandomAccessStream, Windows.Storage.Streams, ContentType = WindowsRuntime] | Out-Null

if (-not (Test-Path $OutDir)) { New-Item -ItemType Directory -Path $OutDir | Out-Null }

$file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync((Resolve-Path $Pdf).Path)) ([Windows.Storage.StorageFile])
$doc = Await ([Windows.Data.Pdf.PdfDocument]::LoadFromFileAsync($file)) ([Windows.Data.Pdf.PdfDocument])

"pages=$($doc.PageCount)"
for ($i = 0; $i -lt $doc.PageCount; $i++) {
    $page = $doc.GetPage($i)
    $out = Join-Path (Resolve-Path $OutDir).Path ("page-{0:d2}.png" -f ($i + 1))
    $fs = [System.IO.File]::Create($out)
    $ras = [System.IO.WindowsRuntimeStreamExtensions]::AsRandomAccessStream($fs)
    $opts = New-Object Windows.Data.Pdf.PdfPageRenderOptions
    $opts.DestinationWidth = [uint32]($page.Size.Width / 72 * $Dpi)
    WaitAction ($page.RenderToStreamAsync($ras, $opts))
    $ras.FlushAsync() | Out-Null
    $fs.Flush(); $fs.Close()
}
"done"
