package org.biomerte.decisions;
import org.json.*;
import java.nio.charset.StandardCharsets;
public final class DecisionModel {
 public static String title(JSONObject r){String value=r.optString("title").trim();if(value.isEmpty())value=r.optString("question","Decision").trim();String[] words=value.split("[\\p{Z}\\s]+");return String.join(" ",java.util.Arrays.copyOf(words,Math.min(10,words.length)));}
 public static String fullQuestion(JSONObject r)throws Exception{String description=describe(r);return r.getString("question")+(description.isEmpty()?"":"\n\n"+description);}
 public static void validate(JSONObject row,String choice,String text)throws Exception{
  if(text==null||text.getBytes(StandardCharsets.UTF_8).length>16384)throw new Exception("Reply exceeds 16 KiB");
  if(choice==null||choice.isEmpty()&&text.trim().isEmpty())throw new Exception("Choose an option or write a reply");
  JSONArray choices=row.getJSONObject("request").getJSONArray("choices");boolean found=choice.isEmpty();for(int n=0;n<choices.length();n++)if(choices.getJSONObject(n).getString("id").equals(choice))found=true;if(!found)throw new Exception("Unknown option");
 }
 public static String describe(JSONObject r)throws Exception{
  StringBuilder out=new StringBuilder();JSONObject p=r.optJSONObject("proposal");if(p==null)return r.optJSONObject("supporting")==null?"":r.getJSONObject("supporting").toString(2);
  append(out,"Proposed action",describeAction(p.getJSONObject("action")));
  String[][] fields={{"targets","Resources"},{"affected_systems","Affected systems"},{"data_operations","Data operations"},{"expected_effects","Expected effects"},{"consequences","Consequences"},{"conditions","Conditions"},{"recovery_actions","Recovery actions"}};
  for(String[] f:fields){JSONArray a=p.getJSONArray(f[0]);if(a.length()==0)continue;StringBuilder values=new StringBuilder();for(int n=0;n<a.length();n++){Object v=a.get(n);values.append("• ").append(v instanceof JSONObject?((JSONObject)v).optString("description",v.toString()):v.toString()).append("\n");}append(out,f[1],values.toString().trim());}
  JSONObject scope=p.getJSONObject("scope");String kind=scope.getString("kind");StringBuilder scopeText=new StringBuilder(kind.equals("single")?"This action once":kind.equals("group")?"This defined group, each action once":"Standing permission for these actions");
  JSONArray actions=scope.optJSONArray("actions");if(actions!=null)for(int n=0;n<actions.length();n++)scopeText.append("\n\n").append(describeAction(actions.getJSONObject(n)));append(out,"Permission scope",scopeText.toString());append(out,"Proposal revision",String.valueOf(p.getInt("revision")));
  if(p.has("expires_at"))append(out,"Expires",new java.util.Date((long)(p.getDouble("expires_at")*1000)).toString());
  if(r.optJSONObject("supporting")!=null&&r.getJSONObject("supporting").length()>0)append(out,"Supporting information",r.getJSONObject("supporting").toString(2));return out.toString();
 }
 private static String describeAction(JSONObject a)throws Exception{StringBuilder text=new StringBuilder(a.getString("contract")).append("\n").append(a.getString("resource"));JSONObject input=a.getJSONObject("inputs");java.util.Iterator<String> keys=input.keys();while(keys.hasNext()){String k=keys.next();text.append("\n").append(k.replace('_',' ')).append(": ").append(input.get(k).toString());}return text.toString();}
 private static void append(StringBuilder out,String title,String value){out.append(title).append("\n").append(value).append("\n\n");}
}
