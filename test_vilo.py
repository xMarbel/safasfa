"""Regression tests for the VILO local backend and Server Studio."""
import os
import sys
import tempfile
import io
from PIL import Image
import unittest
from pathlib import Path
from fastapi.testclient import TestClient

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))

class ViloStudioTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data=tempfile.TemporaryDirectory()
        os.environ['VILO_DATA_DIR']=cls.data.name
        import server
        cls.server=server
        cls.a=TestClient(server.app)
        cls.b=TestClient(server.app)
        cls.need(cls.a.post('/api/register',json={'username':'OwnerVilo','email':'owner@vilo.test','password':'StrongPassword456!'}))
        cls.need(cls.b.post('/api/register',json={'username':'FriendVilo','email':'friend@vilo.test','password':'StrongPassword456!'}))
    @classmethod
    def tearDownClass(cls):
        cls.data.cleanup()
    @staticmethod
    def need(response):
        assert response.status_code<300,(response.status_code,response.text)
        return response.json()
    def test_full_server_workflow(self):
        a,b=self.a,self.b
        rid=self.need(a.post('/api/rooms',json={'name':'VILO Community','kind':'server'}))['id']
        channels=self.need(a.get(f'/api/rooms/{rid}/channels'))
        self.assertEqual(len(channels),2)
        ch=next(c for c in channels if c['name']=='ogólny')
        announcement=next(c for c in channels if c['kind']=='announcement')
        self.assertEqual(b.get(f'/api/rooms/{rid}/channels').status_code,403)
        self.assertEqual(a.post(f'/api/rooms/{rid}/messages',json={'body':'Missing channel'}).status_code,400)
        self.need(a.post(f'/api/rooms/{rid}/messages',json={'body':'Pierwsza wiadomość','channel_id':ch['id']}))
        self.assertEqual(len(self.need(a.get(f'/api/rooms/{rid}/messages?channel_id={ch["id"]}'))),1)
        inv=self.need(a.post(f'/api/rooms/{rid}/invites'))
        self.need(b.post('/api/invites/join',json={'code':inv['code']}))
        self.assertEqual(b.post(f'/api/rooms/{rid}/messages',json={'body':'Nie mogę','channel_id':announcement['id']}).status_code,403)
        self.need(b.post(f'/api/rooms/{rid}/messages',json={'body':'Hej!','channel_id':ch['id']}))
        secret=self.need(a.post(f'/api/rooms/{rid}/channels',json={'name':'moderacja','category':'ZESPÓŁ','staff_only':True}))['id']
        self.assertEqual(b.get(f'/api/rooms/{rid}/messages?channel_id={secret}').status_code,403)
        self.assertNotIn(secret,[x['id'] for x in self.need(b.get(f'/api/rooms/{rid}/channels'))])
        self.assertEqual(b.post(f'/api/rooms/{rid}/channels',json={'name':'hack'}).status_code,403)
        role=self.need(a.post(f'/api/rooms/{rid}/roles',json={'name':'Moderator','color':'#a65aff','view_private':True,'manage_channels':True,'manage_messages':True}))['id']
        user_id=self.need(b.get('/api/me'))['id']
        self.need(a.post(f'/api/rooms/{rid}/roles/{role}/users/{user_id}',json={'assigned':True}))
        self.assertIn(secret,[x['id'] for x in self.need(b.get(f'/api/rooms/{rid}/channels'))])
        private_msg=self.need(b.post(f'/api/rooms/{rid}/messages',json={'body':'Tajna wiadomość','channel_id':secret}))['id']
        # Server cards must not leak private content to other members.
        self.assertNotIn('Tajna wiadomość',str(self.need(a.get('/api/rooms'))))
        self.need(a.patch(f'/api/rooms/{rid}/studio',json={'name':'VILO Community','description':'Czysta społeczność','rules':'Bez spamu.','welcome':'Hej {user}!','accent':'#8066ff'}))
        self.assertEqual(self.need(b.get(f'/api/rooms/{rid}/studio'))['config']['rules'],'Bez spamu.')
        poll=self.need(b.post(f'/api/rooms/{rid}/polls',json={'question':'Głosowanie?','options':['Tak','Nie'],'channel_id':ch['id']}))['id']
        self.assertTrue(poll)
        self.need(a.post(f'/api/rooms/{rid}/bots',json={'name':'Bot','trigger':'/hej','response':'Witaj!'}))
        self.need(b.post(f'/api/rooms/{rid}/messages',json={'body':'/hej','channel_id':ch['id']}))
        self.assertTrue(any(m['kind']=='bot' for m in self.need(b.get(f'/api/rooms/{rid}/messages?channel_id={ch["id"]}'))))
        # Archived private channels must never spill their content into public channels.
        self.need(a.delete(f'/api/rooms/{rid}/channels/{secret}'))
        self.assertEqual(b.get(f'/api/rooms/{rid}/messages?channel_id={secret}').status_code,404)
        self.assertEqual(a.get(f'/api/rooms/{rid}/messages?channel_id={secret}').status_code,404)
        self.assertNotIn('Tajna wiadomość',str(self.need(b.get(f'/api/rooms/{rid}/messages?channel_id={ch["id"]}'))))
        self.assertEqual(a.get(f'/api/uploads/doesnotexist').status_code,404)
    def test_edit_profile_and_images(self):
        a,b=self.a,self.b
        self.need(a.patch('/api/me',json={'display_name':'Nowa nazwa','about':'Opis testowy','status_text':'Zaraz wracam','color':'#7654ab'}))
        me=self.need(a.get('/api/me'))
        self.assertEqual(me['display_name'],'Nowa nazwa')
        self.assertEqual(me['status_text'],'Zaraz wracam')
        self.assertEqual(me['avatar_rev'],0)
        image=Image.new('RGB',(300,300),(121,82,203))
        buf=io.BytesIO();image.save(buf,format='PNG')
        upload=self.need(a.post('/api/me/media/avatar',files={'file':('avatar.png',buf.getvalue(),'image/png')}))
        self.assertGreater(upload['rev'],0)
        me=self.need(a.get('/api/me'))
        self.assertEqual(me['avatar_rev'],upload['rev'])
        view=b.get(f'/api/profile-media/{me["id"]}/avatar')
        self.assertEqual(view.status_code,200)
        self.assertEqual(view.headers['content-type'],'image/webp')
        bad=a.post('/api/me/media/banner',files={'file':('evil.jpg',b'not really an image','image/jpeg')})
        self.assertEqual(bad.status_code,400)
        banner=Image.new('RGB',(600,160),(45,72,135));buf=io.BytesIO();banner.save(buf,format='PNG')
        self.need(a.post('/api/me/media/banner',files={'file':('cover.png',buf.getvalue(),'image/png')}))
        self.assertGreater(self.need(a.get('/api/me'))['banner_rev'],0)
        self.need(a.delete('/api/me/media/avatar'))
        self.assertEqual(a.get(f'/api/profile-media/{me["id"]}/avatar').status_code,404)
        self.assertEqual(self.need(a.get('/api/me'))['avatar_rev'],0)
    def test_group_compatibility(self):
        rid=self.need(self.a.post('/api/rooms',json={'name':'Grupa','kind':'group'}))['id']
        self.need(self.a.post(f'/api/rooms/{rid}/messages',json={'body':'Grupa nadal działa'}))
        messages=self.need(self.a.get(f'/api/rooms/{rid}/messages'))
        self.assertEqual(messages[-1]['body'],'Grupa nadal działa')

if __name__=='__main__':unittest.main()
