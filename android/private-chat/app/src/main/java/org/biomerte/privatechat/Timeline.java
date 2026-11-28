package org.biomerte.privatechat;
import org.json.*;

/** Display order stays separate from text-only inference history. */
final class Timeline {
 static boolean pinLegacyImages(JSONObject data)throws JSONException{
  JSONArray messages=data.optJSONArray("messages"),images=data.optJSONArray("images");
  int end=messages==null?0:messages.length();boolean changed=false;
  if(images!=null)for(int i=0;i<images.length();i++){
   JSONObject image=images.optJSONObject(i);
   if(image!=null&&!image.has("after_message")){image.put("after_message",end);changed=true;}
  }
  return changed;
 }
 static JSONArray items(JSONObject data){
  JSONArray messages=data.optJSONArray("messages"),images=data.optJSONArray("images"),result=new JSONArray();
  int count=messages==null?0:messages.length();
  try{
   for(int at=0;at<=count;at++){
    if(images!=null)for(int i=0;i<images.length();i++){
     JSONObject image=images.optJSONObject(i);
     if(image!=null&&Math.max(0,Math.min(count,image.optInt("after_message",count)))==at)
      result.put(new JSONObject().put("kind","image").put("value",image));
    }
    if(at<count&&messages.optJSONObject(at)!=null)
     result.put(new JSONObject().put("kind","message").put("value",messages.getJSONObject(at)));
   }
  }catch(JSONException e){throw new IllegalStateException(e);}
  return result;
 }
}
