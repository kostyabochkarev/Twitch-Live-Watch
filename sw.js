self.addEventListener('install',event=>{
  event.waitUntil(self.skipWaiting());
});

self.addEventListener('activate',event=>{
  event.waitUntil(self.clients.claim());
});

self.addEventListener('notificationclick',event=>{
  event.notification.close();
  const target=event.notification.data&&event.notification.data.url;
  event.waitUntil((async()=>{
    const windows=await self.clients.matchAll({type:'window',includeUncontrolled:true});
    if(target){
      for(const client of windows){
        if(client.url===target&&'focus' in client){
          await client.focus();
          return;
        }
      }
      await self.clients.openWindow(target);
      return;
    }
    if(windows.length&&'focus' in windows[0]){
      await windows[0].focus();
      return;
    }
    await self.clients.openWindow(self.registration.scope);
  })());
});
