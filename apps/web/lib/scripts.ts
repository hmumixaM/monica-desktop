const scripts=new Map<string,Promise<void>>();
export function loadScript(src:string){if(!scripts.has(src))scripts.set(src,new Promise((resolve,reject)=>{const tag=document.createElement('script');tag.src=src;tag.onload=()=>resolve();tag.onerror=()=>{scripts.delete(src);reject(new Error(src));};document.body.append(tag);}));return scripts.get(src)!;}
