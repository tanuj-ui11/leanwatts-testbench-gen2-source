using System;
using System.IO;
using System.Reflection;
using System.Reflection.Emit;
using System.Collections.Generic;
class DumpIL {
 static Dictionary<ushort,OpCode> ops=new Dictionary<ushort,OpCode>();
 static Assembly asm;
 static string Resolve(Module mod, int token, OperandType type) {
   try { if(type==OperandType.InlineString) return "\""+mod.ResolveString(token).Replace("\r","\\r").Replace("\n","\\n")+"\"";
   if(type==OperandType.InlineSig) return "signature "+token.ToString("X8");
   MemberInfo m=mod.ResolveMember(token); return (m.DeclaringType==null?"":m.DeclaringType.FullName+"::")+m.ToString(); }
   catch(Exception e) {return "token="+token.ToString("X8")+" ["+e.Message+"]";} }
 static void Main(string[] args) {
   string dir=Path.GetDirectoryName(Path.GetFullPath(args[0]));
   AppDomain.CurrentDomain.ReflectionOnlyAssemblyResolve+=(s,e)=> { string f=Path.Combine(dir,new AssemblyName(e.Name).Name+".dll"); return File.Exists(f)?Assembly.ReflectionOnlyLoadFrom(f):Assembly.ReflectionOnlyLoad(e.Name); };
   foreach(FieldInfo f in typeof(OpCodes).GetFields(BindingFlags.Public|BindingFlags.Static)) {OpCode o=(OpCode)f.GetValue(null);ops[unchecked((ushort)o.Value)]=o;}
   asm=Assembly.ReflectionOnlyLoadFrom(args[0]);
   Console.WriteLine("Assembly: "+asm.FullName);
   foreach(Type t in asm.GetTypes()) {
     Console.WriteLine("\nTYPE "+t.FullName);
     foreach(FieldInfo f in t.GetFields(BindingFlags.Public|BindingFlags.NonPublic|BindingFlags.Static|BindingFlags.Instance|BindingFlags.DeclaredOnly)) Console.WriteLine("FIELD "+f);
     List<MethodBase> ms=new List<MethodBase>();ms.AddRange(t.GetMethods(BindingFlags.Public|BindingFlags.NonPublic|BindingFlags.Static|BindingFlags.Instance|BindingFlags.DeclaredOnly));ms.AddRange(t.GetConstructors(BindingFlags.Public|BindingFlags.NonPublic|BindingFlags.Static|BindingFlags.Instance));
     foreach(MethodBase m in ms) {
       Console.WriteLine("\nMETHOD "+t.FullName+"::"+m+" TOKEN "+m.MetadataToken.ToString("X8"));
       foreach(CustomAttributeData a in m.GetCustomAttributesData()) Console.WriteLine("ATTRIBUTE "+a);
       MethodBody body=m.GetMethodBody();if(body==null)continue;
       foreach(LocalVariableInfo v in body.LocalVariables)Console.WriteLine("LOCAL "+v.LocalIndex+" "+v.LocalType);
       byte[] b=body.GetILAsByteArray();int p=0;
       while(p<b.Length) {
         int start=p;ushort val=b[p++];if(val==254)val=(ushort)(0xFE00+b[p++]);OpCode o=ops[val];object operand="";
         switch(o.OperandType) {
           case OperandType.InlineNone:break;
           case OperandType.ShortInlineI:operand=(sbyte)b[p++];break;
           case OperandType.ShortInlineVar:operand=b[p++];break;
           case OperandType.InlineVar:operand=BitConverter.ToUInt16(b,p);p+=2;break;
           case OperandType.InlineI:operand=BitConverter.ToInt32(b,p);p+=4;break;
           case OperandType.InlineI8:operand=BitConverter.ToInt64(b,p);p+=8;break;
           case OperandType.ShortInlineR:operand=BitConverter.ToSingle(b,p);p+=4;break;
           case OperandType.InlineR:operand=BitConverter.ToDouble(b,p);p+=8;break;
           case OperandType.ShortInlineBrTarget:operand="IL_"+(p+1+(sbyte)b[p]).ToString("X4");p++;break;
           case OperandType.InlineBrTarget:operand="IL_"+(p+4+BitConverter.ToInt32(b,p)).ToString("X4");p+=4;break;
           case OperandType.InlineSwitch:int n=BitConverter.ToInt32(b,p);p+=4;int end=p+4*n;string[] targets=new string[n];for(int i=0;i<n;i++){targets[i]="IL_"+(end+BitConverter.ToInt32(b,p)).ToString("X4");p+=4;}operand=string.Join(", ",targets);break;
           default:int token=BitConverter.ToInt32(b,p);p+=4;operand=Resolve(m.Module,token,o.OperandType);break;
         }
         Console.WriteLine("IL_"+start.ToString("X4")+": "+o.Name+" "+operand);
       }
     }
   }
 }
}
