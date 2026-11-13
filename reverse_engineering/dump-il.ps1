$ErrorActionPreference = 'Stop'
$vendor = Join-Path $PSScriptRoot '../JKCAN_CHARGE/GBS_CAN上位机.exe'
$vendorDir = Split-Path $vendor
[AppDomain]::CurrentDomain.add_ReflectionOnlyAssemblyResolve({
 param($sender,$eventArgs)
 $name = New-Object System.Reflection.AssemblyName($eventArgs.Name)
 $candidate = Join-Path $vendorDir ($name.Name+'.dll')
 if (Test-Path $candidate) { return [Reflection.Assembly]::ReflectionOnlyLoadFrom($candidate) }
 return [Reflection.Assembly]::ReflectionOnlyLoad($eventArgs.Name)
})
$ops = @{}
foreach ($f in [Reflection.Emit.OpCodes].GetFields('Public,Static')) {
 $o = $f.GetValue($null)
 $ops[[int]$o.Value -band 65535] = $o
}
$assembly = [Reflection.Assembly]::ReflectionOnlyLoadFrom($vendor)
$output = New-Object IO.StreamWriter((Join-Path $PSScriptRoot 'vendor-il.txt'),$false,[Text.Encoding]::UTF8)
try {
 foreach ($type in $assembly.GetTypes()) {
  $output.WriteLine("`nTYPE $($type.FullName)")
  foreach ($f in $type.GetFields('Public,NonPublic,Instance,Static,DeclaredOnly')) { $output.WriteLine("FIELD $f") }
  $methods = @($type.GetMethods('Public,NonPublic,Instance,Static,DeclaredOnly')) + @($type.GetConstructors('Public,NonPublic,Instance,Static'))
  foreach ($method in $methods) {
   $output.WriteLine("`nMETHOD $($type.FullName)::$method TOKEN $($method.MetadataToken.ToString('X8'))")
   foreach($attribute in $method.GetCustomAttributesData()) { $output.WriteLine("ATTRIBUTE $attribute") }
   $body = $method.GetMethodBody()
   if ($null -eq $body) { continue }
   foreach($v in $body.LocalVariables) { $output.WriteLine("LOCAL $($v.LocalIndex) $($v.LocalType)") }
   $bytes=$body.GetILAsByteArray(); $pos=0
   while($pos -lt $bytes.Length) {
    $start=$pos; $value=[int]$bytes[$pos];$pos++
    if($value -eq 254){$value=65280+[int]$bytes[$pos];$pos++}
    $op=$ops[$value];$operand=''
    switch($op.OperandType.ToString()) {
     InlineNone { }
     ShortInlineI { $operand=[int]$bytes[$pos];if($operand -gt 127){$operand-=256};$pos++ }
     ShortInlineVar { $operand=$bytes[$pos];$pos++ }
     InlineVar { $operand=[BitConverter]::ToUInt16($bytes,$pos);$pos+=2 }
     InlineI { $operand=[BitConverter]::ToInt32($bytes,$pos);$pos+=4 }
     InlineI8 { $operand=[BitConverter]::ToInt64($bytes,$pos);$pos+=8 }
     ShortInlineR { $operand=[BitConverter]::ToSingle($bytes,$pos);$pos+=4 }
     InlineR { $operand=[BitConverter]::ToDouble($bytes,$pos);$pos+=8 }
     ShortInlineBrTarget { $offset=[int]$bytes[$pos];if($offset -gt 127){$offset-=256};$operand='IL_'+($pos+1+$offset).ToString('X4');$pos++ }
     InlineBrTarget { $operand='IL_'+($pos+4+[BitConverter]::ToInt32($bytes,$pos)).ToString('X4');$pos+=4 }
     InlineSwitch { $count=[BitConverter]::ToInt32($bytes,$pos);$pos+=4;$end=$pos+4*$count;$targets=@();for($i=0;$i -lt $count;$i++){$targets+='IL_'+($end+[BitConverter]::ToInt32($bytes,$pos)).ToString('X4');$pos+=4};$operand=$targets -join ', ' }
     default {
      $token=[BitConverter]::ToInt32($bytes,$pos);$pos+=4
      try {
       if($op.OperandType.ToString() -eq 'InlineString'){$operand='"'+$method.Module.ResolveString($token).Replace("`r",'\r').Replace("`n",'\n')+'"'}
       elseif($op.OperandType.ToString() -eq 'InlineSig'){$operand='SIGNATURE '+$token.ToString('X8')}
       else {$m=$method.Module.ResolveMember($token);$operand=$m.DeclaringType.FullName+'::'+$m.ToString()}
      } catch {$operand='TOKEN '+$token.ToString('X8')+' '+$_.Exception.Message}
     }
    }
    $output.WriteLine(('IL_{0:X4}: {1} {2}' -f $start,$op.Name,$operand))
   }
  }
 }
} finally { $output.Dispose() }
