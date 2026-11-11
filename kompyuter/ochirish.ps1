# Davomat - kompyuter versiyasini o'chirish (yorliqlar va avtomatik ochilishni olib tashlaydi)
$desktop  = [Environment]::GetFolderPath('Desktop')
$programs = Join-Path ([Environment]::GetFolderPath('StartMenu')) 'Programs'
$startup  = [Environment]::GetFolderPath('Startup')
foreach ($dir in @($desktop, $programs, $startup)) {
    Remove-Item (Join-Path $dir 'Davomat admin.lnk')  -ErrorAction SilentlyContinue
    Remove-Item (Join-Path $dir 'Davomat ekrani.lnk') -ErrorAction SilentlyContinue
}
$ans = Read-Host "  Kirish ma'lumotlari ham o'chirilsinmi? (ha / yo'q)"
if ($ans -match '^h') { Remove-Item (Join-Path $env:LOCALAPPDATA 'DavomatApp') -Recurse -Force -ErrorAction SilentlyContinue }
Write-Host ''
Write-Host '  Davomat yorliqlari va avtomatik ochilish olib tashlandi.' -ForegroundColor Green
Start-Sleep -Seconds 3
