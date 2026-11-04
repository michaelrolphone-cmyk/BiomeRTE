package org.biomerte.decisions;
import org.json.*;
import java.nio.charset.StandardCharsets;
public final class DecisionModel {
 public static void validate(JSONObject row,String choice,String text)throws Exception{
  if(text.getBytes(StandardCharsets.UTF_8).length>16384)throw new Exception("Reply exceeds 16 KiB");
  if(choice.isEmpty()&&text.trim().isEmpty())throw new Exception("Choose an option or write a reply");
  JSONArray choices=row.getJSONObject("request").getJSONArray("choices");boolean found=choice.isEmpty();for(int n=0;n<choices.length();n++)if(choices.getJSONObject(n).getString("id").equals(choice))found=true;if(!found)throw new Exception("Unknown option");
 }
 public static String describe(JSONObject r)throws Exception{
  StringBuilder out=new StringBuilder();JSONObject p=r.optJSONObject("proposal");if(p==null)return r.optJSONObject("supporting")==null?"":r.getJSONObject("supporting").toString(2);
  append(out,"Action",p.getJSONObject("action").toString(2));
  for(String key:new String[]{"targets","affected_systems","data_operations","expected_effects","consequences","conditions","recovery_actions"})append(out,key.replace('_',' '),p.getJSONArray(key).toString(2));
  append(out,"Permission scope",p.getJSONObject("scope").toString(2));append(out,"Proposal revision",String.valueOf(p.getInt("revision")));
  if(p.has("expires_at"))append(out,"Expires",new java.util.Date((long)(p.getDouble("expires_at")*1000)).toString());
  if(r.optJSONObject("supporting")!=null)append(out,"Supporting information",r.getJSONObject("supporting").toString(2));return out.toString();
 }
 private static void append(StringBuilder out,String title,String value){out.append(title).append("\n").append(value).append("\n\n");}
}
