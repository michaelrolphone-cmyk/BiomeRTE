package org.biomerte.privatechat;
import org.json.*;
import java.util.*;
final class Models {
 static JSONArray unique(JSONArray source){
  LinkedHashMap<String,JSONObject> models=new LinkedHashMap<>();
  if(source!=null)for(int i=0;i<source.length();i++){JSONObject m=source.optJSONObject(i);if(m==null)continue;String key=m.optString("id").trim();if(key.isEmpty())continue;
   JSONObject old=models.get(key);try{if(old==null)models.put(key,new JSONObject(m.toString()));else if(m.optBoolean("loaded"))old.put("loaded",true);}catch(JSONException ignored){}
  }
  JSONArray result=new JSONArray();for(JSONObject m:models.values())result.put(m);return result;
 }
}
