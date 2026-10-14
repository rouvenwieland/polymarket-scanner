# Aufruf: powershell -File run.ps1 DataWriteGroup DataWriteGroup_gruppe.cfg
param([string]$Module, [string]$Cfg)
$java = "C:\Program Files\Eclipse Adoptium\jre-21.0.12.101-hotspot\bin\java.exe"
$jar = "C:\Users\Nevuor\Tools\tla\tla2tools.jar"
$meta = Join-Path $env:TEMP "tlastates"
& $java -XX:+UseParallelGC -cp $jar tlc2.TLC -config $Cfg -workers auto -metadir $meta "$Module.tla" 2>&1
