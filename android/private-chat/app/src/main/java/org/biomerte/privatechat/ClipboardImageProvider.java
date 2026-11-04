package org.biomerte.privatechat;
import android.content.*;
import android.database.*;
import android.net.Uri;
import android.os.*;
import android.provider.OpenableColumns;
import java.io.*;

/** Android grants pasted URI access; no exported provider or public image file. */
public final class ClipboardImageProvider extends ContentProvider {
 @Override public boolean onCreate(){return true;}
 @Override public String getType(Uri uri){try{ClipboardImages.file(getContext(),uri);return "image/png";}catch(Exception e){return null;}}
 @Override public ParcelFileDescriptor openFile(Uri uri,String mode)throws FileNotFoundException{
  if(!"r".equals(mode))throw new FileNotFoundException("Read only");
  final byte[] png;try{png=ClipboardImages.read(getContext(),uri);}catch(Exception e){throw new FileNotFoundException("Clipboard image unavailable");}
  final ParcelFileDescriptor[] pipe;try{pipe=ParcelFileDescriptor.createPipe();}catch(IOException e){Crypto.wipe(png);throw new FileNotFoundException();}
  Thread writer=new Thread(()->{try(OutputStream out=new ParcelFileDescriptor.AutoCloseOutputStream(pipe[1])){out.write(png);}catch(IOException ignored){}finally{Crypto.wipe(png);}},"clipboard-image");writer.start();return pipe[0];
 }
 @Override public Cursor query(Uri uri,String[] projection,String selection,String[] args,String sort){try{File f=ClipboardImages.file(getContext(),uri);if(!f.isFile())return null;String[] columns=projection==null?new String[]{OpenableColumns.DISPLAY_NAME,OpenableColumns.SIZE}:projection;MatrixCursor c=new MatrixCursor(columns);Object[] values=new Object[columns.length];for(int i=0;i<columns.length;i++){if(OpenableColumns.DISPLAY_NAME.equals(columns[i]))values[i]="biome-image.png";else if(OpenableColumns.SIZE.equals(columns[i]))values[i]=Math.max(0,f.length()-28);}c.addRow(values);return c;}catch(Exception e){return null;}}
 @Override public Uri insert(Uri uri,ContentValues values){throw new UnsupportedOperationException();}
 @Override public int update(Uri uri,ContentValues values,String selection,String[] args){throw new UnsupportedOperationException();}
 @Override public int delete(Uri uri,String selection,String[] args){throw new UnsupportedOperationException();}
}
