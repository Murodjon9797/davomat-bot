# Davomat - kompyuter versiyasini o'rnatish
# Ish stoli va Start menyusiga yorliq qo'yadi, xohlasangiz kompyuter yoqilganda avtomatik ochadi.
# Hech narsa yuklab olinmaydi: Windows'dagi Microsoft Edge (yoki Chrome) "ilova oynasi" rejimida ishlatiladi.

$ErrorActionPreference = 'Stop'
$Url     = 'https://davomat-bot-4ci8.onrender.com/'
$AppDir  = Join-Path $env:LOCALAPPDATA 'DavomatApp'
$Profile = Join-Path $AppDir 'profil'
$Icon    = Join-Path $AppDir 'davomat.ico'

function Find-Browser {
    $paths = @(
        "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe",
        "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe",
        "$env:LOCALAPPDATA\Microsoft\Edge\Application\msedge.exe",
        "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
        "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
        "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"
    )
    foreach ($p in $paths) { if ($p -and (Test-Path $p)) { return $p } }
    return $null
}

function New-Shortcut($path, $target, $arguments, $description) {
    $sh = New-Object -ComObject WScript.Shell
    $lnk = $sh.CreateShortcut($path)
    $lnk.TargetPath = $target
    $lnk.Arguments = $arguments
    $lnk.Description = $description
    $lnk.WorkingDirectory = $AppDir
    if (Test-Path $Icon) { $lnk.IconLocation = "$Icon,0" }
    $lnk.Save()
}

Write-Host ''
Write-Host '  ==============================================' -ForegroundColor Cyan
Write-Host '     DAVOMAT - kompyuter versiyasini o''rnatish' -ForegroundColor Cyan
Write-Host '  ==============================================' -ForegroundColor Cyan
Write-Host ''

$browser = Find-Browser
if (-not $browser) {
    Write-Host '  Microsoft Edge yoki Google Chrome topilmadi. Avval ulardan birini o''rnating.' -ForegroundColor Red
    Read-Host '  Chiqish uchun Enter bosing'
    exit 1
}
Write-Host "  Brauzer: $browser" -ForegroundColor DarkGray

New-Item -ItemType Directory -Force -Path $AppDir | Out-Null
$srcIcon = Join-Path $PSScriptRoot 'davomat.ico'
if (Test-Path $srcIcon) { Copy-Item $srcIcon $Icon -Force }

$common    = "--user-data-dir=`"$Profile`" --no-first-run --no-default-browser-check --disable-features=Translate"
$argsAdmin = "--app=`"$Url`" --start-maximized $common"
$argsScreen = "--app=`"$($Url)?screen=1`" --start-fullscreen $common"

Write-Host ''
Write-Host '  Kompyuter yoqilganda nima avtomatik ochilsin?' -ForegroundColor Yellow
Write-Host ''
Write-Host '   1 - Ekran rejimi: katta QR kod va "hozir darsda" ro''yxati (kirish joyidagi monitor uchun)'
Write-Host '   2 - Admin panel (direktor / o''quv bo''limi kompyuteri uchun)'
Write-Host '   3 - Hech narsa, faqat ish stoliga yorliq qo''yilsin'
Write-Host ''
$choice = Read-Host '  Tanlang (1, 2 yoki 3)'

$desktop  = [Environment]::GetFolderPath('Desktop')
$programs = Join-Path ([Environment]::GetFolderPath('StartMenu')) 'Programs'
$startup  = [Environment]::GetFolderPath('Startup')

New-Shortcut (Join-Path $desktop  'Davomat admin.lnk')  $browser $argsAdmin  'Davomat - admin panel'
New-Shortcut (Join-Path $desktop  'Davomat ekrani.lnk') $browser $argsScreen 'Davomat - ekran rejimi (QR)'
New-Shortcut (Join-Path $programs 'Davomat admin.lnk')  $browser $argsAdmin  'Davomat - admin panel'
New-Shortcut (Join-Path $programs 'Davomat ekrani.lnk') $browser $argsScreen 'Davomat - ekran rejimi (QR)'

Remove-Item (Join-Path $startup 'Davomat*.lnk') -ErrorAction SilentlyContinue
switch ($choice) {
    '1' { New-Shortcut (Join-Path $startup 'Davomat ekrani.lnk') $browser $argsScreen 'Davomat - ekran rejimi (QR)'
          $run = $argsScreen; $msg = 'Ekran rejimi kompyuter yoqilganda avtomatik ochiladi.' }
    '2' { New-Shortcut (Join-Path $startup 'Davomat admin.lnk') $browser $argsAdmin 'Davomat - admin panel'
          $run = $argsAdmin; $msg = 'Admin panel kompyuter yoqilganda avtomatik ochiladi.' }
    default { $run = $argsAdmin; $msg = 'Avtomatik ochilish yoqilmadi.' }
}

Write-Host ''
Write-Host '  Tayyor!' -ForegroundColor Green
Write-Host "  - Ish stolida 'Davomat admin' va 'Davomat ekrani' yorliqlari paydo bo'ldi."
Write-Host "  - $msg"
Write-Host '  - Birinchi ochilganda Telegram username yoki ID ni kiriting: bot kirish kodini yuboradi.'
Write-Host '    Keyingi safar qayta kod so''ralmaydi (180 kun).'
Write-Host '  - Ekran rejimidan chiqish: Esc yoki F11. O''chirish uchun: ochirish.bat'
Write-Host ''

Start-Process -FilePath $browser -ArgumentList $run
Start-Sleep -Seconds 2
