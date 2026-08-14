Set-Location $PSScriptRoot
$env:TOKEN_DASHBOARD_BOX = 'BMF'
py -3 cli.py dashboard
