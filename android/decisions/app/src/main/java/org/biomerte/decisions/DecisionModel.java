package org.biomerte.decisions;

import org.json.*;
import java.nio.charset.StandardCharsets;
import java.util.HashSet;
import java.util.Set;

/** Validate what the user must be able to review before a reply can be signed. */
public final class DecisionModel {
 public static String problem(Object value) {
  try {
   if(!(value instanceof JSONObject))throw new Exception("Decision must be an object");
   requireValid((JSONObject)value);
   return "";
  } catch(Exception error) {
   return error.getMessage()==null?"Invalid decision":error.getMessage();
  }
 }

 public static void requireValid(JSONObject row)throws Exception {
  string(row,"id"); string(row,"digest");
  JSONObject request=object(row,"request");
  string(request,"question");
  int revision=revision(request,"proposal_revision");
  JSONArray choices=array(request,"choices");
  if(choices.length()==0)throw new Exception("Decision choices are missing");
  Set<String> ids=new HashSet<>();
  for(int n=0;n<choices.length();n++){
   JSONObject choice=choices.optJSONObject(n);
   if(choice==null)throw new Exception("Decision choice must be an object");
   if(!ids.add(string(choice,"id")))throw new Exception("Decision choice IDs must be unique");
   string(choice,"label");
  }
  if(request.has("proposal"))proposal(object(request,"proposal"),revision);
  else if(request.has("authorization_request"))throw new Exception("Authorization proposal is missing");
  if(request.has("supporting")&&!(request.get("supporting") instanceof JSONObject))
   throw new Exception("Supporting information must be an object");
 }

 private static String string(JSONObject value,String key)throws Exception {
  Object found=value.opt(key);
  if(!(found instanceof String)||((String)found).trim().isEmpty())
   throw new Exception("Missing or invalid "+key);
  return (String)found;
 }
 private static JSONObject object(JSONObject value,String key)throws Exception {
  JSONObject found=value.optJSONObject(key);
  if(found==null)throw new Exception("Missing or invalid "+key+" object");
  return found;
 }
 private static JSONArray array(JSONObject value,String key)throws Exception {
  JSONArray found=value.optJSONArray(key);
  if(found==null)throw new Exception("Missing or invalid "+key+" list");
  return found;
 }
 private static int revision(JSONObject value,String key)throws Exception {
  Object found=value.opt(key);
  if(!(found instanceof Number))throw new Exception("Missing or invalid "+key);
  double number=((Number)found).doubleValue();
  if(!Double.isFinite(number)||number<1||number>Integer.MAX_VALUE||number!=Math.rint(number))
   throw new Exception("Missing or invalid "+key);
  return (int)number;
 }
 private static void action(JSONObject value)throws Exception {
  string(value,"contract"); string(value,"resource"); object(value,"inputs");
 }
 private static void proposal(JSONObject value,int expectedRevision)throws Exception {
  if(revision(value,"revision")!=expectedRevision)throw new Exception("Proposal revision does not match the decision");
  action(object(value,"action"));
  for(String key:new String[]{"targets","affected_systems","data_operations","expected_effects","consequences","conditions","recovery_actions"})
   array(value,key);
  JSONObject scope=object(value,"scope");
  String kind=string(scope,"kind");
  if(!kind.equals("single")&&!kind.equals("group")&&!kind.equals("standing"))
   throw new Exception("Unknown permission scope");
  if(!kind.equals("single")||scope.has("actions")){
   JSONArray actions=array(scope,"actions");
   if(!kind.equals("single")&&actions.length()==0)throw new Exception("Permission scope actions are missing");
   for(int n=0;n<actions.length();n++){
    JSONObject descriptor=actions.optJSONObject(n);
    if(descriptor==null)throw new Exception("Permission scope action must be an object");
    action(descriptor);
   }
  }
  if(value.has("expires_at")){
   Object expires=value.get("expires_at");
   if(!(expires instanceof Number)||!Double.isFinite(((Number)expires).doubleValue()))
    throw new Exception("Invalid permission expiration");
  }
 }

 public static void validate(JSONObject row,String choice,String text)throws Exception {
  requireValid(row);
  if(text==null||text.getBytes(StandardCharsets.UTF_8).length>16384)throw new Exception("Reply exceeds 16 KiB");
  if(choice==null||(choice.isEmpty()&&text.trim().isEmpty()))throw new Exception("Choose an option or write a reply");
  JSONArray choices=row.getJSONObject("request").getJSONArray("choices");
  boolean found=choice.isEmpty();
  for(int n=0;n<choices.length();n++)if(choices.getJSONObject(n).getString("id").equals(choice))found=true;
  if(!found)throw new Exception("Unknown option");
 }

 public static String describe(JSONObject request)throws Exception {
  StringBuilder out=new StringBuilder();
  JSONObject p=request.optJSONObject("proposal");
  if(p==null)return request.optJSONObject("supporting")==null?"":request.getJSONObject("supporting").toString(2);
  proposal(p,revision(request,"proposal_revision"));
  append(out,"Proposed action",describeAction(p.getJSONObject("action")));
  String[][] fields={{"targets","Resources"},{"affected_systems","Affected systems"},{"data_operations","Data operations"},{"expected_effects","Expected effects"},{"consequences","Consequences"},{"conditions","Conditions"},{"recovery_actions","Recovery actions"}};
  for(String[] field:fields){
   JSONArray values=p.getJSONArray(field[0]); if(values.length()==0)continue;
   StringBuilder text=new StringBuilder();
   for(int n=0;n<values.length();n++){
    Object value=values.get(n);
    text.append("• ").append(value instanceof JSONObject?((JSONObject)value).optString("description",value.toString()):value.toString()).append("\n");
   }
   append(out,field[1],text.toString().trim());
  }
  JSONObject scope=p.getJSONObject("scope");
  String kind=scope.getString("kind");
  StringBuilder scopeText=new StringBuilder(kind.equals("single")?"This action once":kind.equals("group")?"This defined group, each action once":"Standing permission for these actions");
  JSONArray actions=scope.optJSONArray("actions");
  if(actions!=null)for(int n=0;n<actions.length();n++)scopeText.append("\n\n").append(describeAction(actions.getJSONObject(n)));
  append(out,"Permission scope",scopeText.toString());
  append(out,"Proposal revision",String.valueOf(p.getInt("revision")));
  if(p.has("expires_at"))append(out,"Expires",new java.util.Date((long)(p.getDouble("expires_at")*1000)).toString());
  if(request.optJSONObject("supporting")!=null&&request.getJSONObject("supporting").length()>0)
   append(out,"Supporting information",request.getJSONObject("supporting").toString(2));
  return out.toString();
 }
 private static String describeAction(JSONObject action)throws Exception {
  StringBuilder out=new StringBuilder(action.getString("contract")).append("\n").append(action.getString("resource"));
  JSONObject inputs=action.getJSONObject("inputs");
  java.util.Iterator<String> keys=inputs.keys();
  while(keys.hasNext()){String key=keys.next();out.append("\n").append(key.replace('_',' ')).append(": ").append(inputs.get(key).toString());}
  return out.toString();
 }
 private static void append(StringBuilder out,String title,String value){out.append(title).append("\n").append(value).append("\n\n");}
}
