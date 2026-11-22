// Supabase session is held only in this object; no localStorage or cookies.
export class AuthClient {
  constructor(config,onExpired){this.config=config;this.onExpired=onExpired;this.session=null;this.timer=null;this.generation=0;}
  async tokenRequest(grant,body,generation=this.generation){
    const response=await fetch(`${this.config.supabase_url}/auth/v1/token?grant_type=${grant}`,{
      method:'POST',headers:{'Content-Type':'application/json',apikey:this.config.supabase_publishable_key},
      body:JSON.stringify(body),redirect:'error'});
    if(!response.ok)throw new Error('Supabase sign-in failed. Check your account details and project configuration.');
    const session=await response.json();
    if(typeof session.access_token!=='string'||typeof session.refresh_token!=='string')throw new Error('Supabase returned an incomplete session.');
    if(generation!==this.generation)throw new Error('Session changed while signing in.');
    this.session=session;clearTimeout(this.timer);
    this.timer=setTimeout(()=>this.refresh(),Math.max(30,(Number(session.expires_in)||3600)-60)*1000);
    return session;
  }
  async signIn(email,password){this.clear();return this.tokenRequest('password',{email,password},this.generation);}
  async refresh(){
    const generation=this.generation;
    try{if(this.session)await this.tokenRequest('refresh_token',{refresh_token:this.session.refresh_token},generation);}
    catch{if(generation===this.generation){this.clear();this.onExpired();}}
  }
  clear(){this.generation++;clearTimeout(this.timer);this.timer=null;this.session=null;}
  async signOut(){
    const token=this.session?.access_token;this.clear();
    if(token){try{await fetch(`${this.config.supabase_url}/auth/v1/logout`,{method:'POST',headers:{apikey:this.config.supabase_publishable_key,Authorization:`Bearer ${token}`},redirect:'error'});}catch{/* Local sign-out always clears the in-memory session. */}}
  }
}
