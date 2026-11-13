param([switch]$CheckOnly)
# Independent Windows/.NET serial check. Only three fixed read queries.
# No GUI, Python driver, instrument setting writes, retries or port scanning.
$ErrorActionPreference = 'Stop'

function Get-RtuCrc([byte[]]$Bytes) {
    [int]$crcValue = 65535
    foreach ($octet in $Bytes) {
        $crcValue = $crcValue -bxor [int]$octet
        for ($bitIndex = 0; $bitIndex -lt 8; $bitIndex++) {
            if ($crcValue -band 1) { $crcValue = ($crcValue -shr 1) -bxor 40961 }
            else { $crcValue = $crcValue -shr 1 }
        }
    }
    return $crcValue
}

function Get-Hex([byte[]]$Bytes) {
    return [BitConverter]::ToString($Bytes).Replace('-', ' ')
}

function Read-Bounded([System.IO.Ports.SerialPort]$Connection, [int]$Milliseconds) {
    $received = New-Object 'System.Collections.Generic.List[byte]'
    $elapsed = [Diagnostics.Stopwatch]::StartNew()
    while ($elapsed.ElapsedMilliseconds -lt $Milliseconds -and $received.Count -lt 128) {
        try {
            $value = $Connection.ReadByte()
            if ($value -ge 0) { $received.Add([byte]$value) }
        } catch [System.TimeoutException] { }
    }
    return ,$received.ToArray()
}

$queries = @(
    @{ name='voltage'; bytes=[byte[]](0x01,0x03,0x0B,0x00,0x00,0x02,0xC6,0x2F); data_count=4 },
    @{ name='model'; bytes=[byte[]](0x01,0x03,0x0B,0x06,0x00,0x01,0x66,0x2F); data_count=2 },
    @{ name='input_state'; bytes=[byte[]](0x01,0x01,0x05,0x10,0x00,0x01,0xFC,0xC3); data_count=1 }
)
foreach ($query in $queries) {
    if ((Get-RtuCrc $query.bytes) -ne 0 -or $query.bytes[1] -notin @(1,3)) {
        throw 'Read-query self-check failed; no port will be opened.'
    }
}
$connection = New-Object System.IO.Ports.SerialPort
$connection.PortName = 'COM3'
$connection.BaudRate = 9600
$connection.Parity = [System.IO.Ports.Parity]::None
$connection.DataBits = 8
$connection.StopBits = [System.IO.Ports.StopBits]::One
$connection.Handshake = [System.IO.Ports.Handshake]::None
$connection.DtrEnable = $true
$connection.RtsEnable = $true
$connection.DiscardNull = $false
$connection.ReadTimeout = 50
$connection.WriteTimeout = 500
if ($CheckOnly) {
    $connection.Dispose()
    'Read queries and independent serial API checked; no hardware accessed.'
    exit 0
}
if ('COM3' -notin [System.IO.Ports.SerialPort]::GetPortNames()) {
    throw 'COM3 is absent; no port was opened.'
}
$observations = New-Object 'System.Collections.Generic.List[object]'
$report = [ordered]@{
    checked_at=[DateTime]::UtcNow.ToString('o'); implementation='Windows .NET System.IO.Ports.SerialPort'
    port='COM3'; baudrate=9600; parity='N'; data_bits=8; stop_bits=1; handshake='None'
    dtr=$true; rts=$true; instrument_writes=$false; idle_rx_hex=''; transactions=@()
}
try {
    $connection.Open()
    $report.idle_rx_hex = Get-Hex (Read-Bounded $connection 250)
    foreach ($query in $queries) {
        $connection.DiscardInBuffer()
        $connection.Write($query.bytes, 0, $query.bytes.Length)
        [byte[]]$received = Read-Bounded $connection 1000
        $crcValid = $received.Length -ge 4 -and (Get-RtuCrc $received) -eq 0
        $validReply = $received.Length -eq ($query.data_count + 5) -and $crcValid -and
            $received[0] -eq 1 -and $received[1] -eq $query.bytes[1] -and $received[2] -eq $query.data_count
        $observations.Add([ordered]@{
            query=$query.name; tx_hex=(Get-Hex $query.bytes); rx_hex=(Get-Hex $received)
            whole_rx_crc_valid=$crcValid; valid_expected_reply=$validReply
            exact_request_echo=((Get-Hex $received) -eq (Get-Hex $query.bytes))
        })
    }
} catch {
    $report['error'] = $_.Exception.Message
} finally {
    $connection.Dispose()
}
$report.transactions = $observations.ToArray()
$destination = Join-Path (Split-Path $PSScriptRoot -Parent) ('artifacts\local-load-windows-' + [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffZ') + '.json')
$report | ConvertTo-Json -Depth 7 | Set-Content -LiteralPath $destination -Encoding UTF8
$report | ConvertTo-Json -Depth 7
'Saved: ' + $destination
