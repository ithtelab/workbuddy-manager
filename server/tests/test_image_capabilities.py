import unittest
from server.services import tencent, modelcatalog

class ImageCapabilityTests(unittest.TestCase):
 def test_official_boolean_only(self):
  for value, expected in [(True,True),(False,False),(None,None),("false",None),(1,None)]:
   with self.subTest(value=value):
    items,_=tencent._parse_model_payload({"models":[{"id":"example","supportsImages":value,"maxOutputTokens":4096}]},"cn")
    self.assertIs(items["example"].get("supports_images"),expected)
 def test_catalog_preserves_unknown(self):
  for value in [None,"false",1]:
   self.assertIsNone(modelcatalog._decorate([{"id":"example","supports_images":value}])[0]["supports_images"])
 def test_fallback_preserves_unknown(self):
  self.assertIsNone(modelcatalog._map_upstream_model_fields({"id":"example","supports_images":"false"})["supports_images"])


 def test_conflicting_official_sources(self):
  result=tencent._image_capability({'supports_images':False},{'supports_images':True})
  self.assertIsNone(result['supports_images'])
  self.assertTrue(result['image_input_conflict'])
  self.assertEqual(result['image_input_sources'],{'enterprise_models':False,'v3_config':True})
 def test_missing_is_not_a_disagreement(self):
  for value in [True,False]:
   result=tencent._image_capability({'supports_images':value},{})
   self.assertIs(result['supports_images'],value)
   self.assertFalse(result['image_input_conflict'])
 def test_both_sources_missing(self):
  self.assertIsNone(tencent._image_capability(None,{})['supports_images'])

 def test_fetch_keeps_conflict_through_catalog(self):
  import asyncio
  from unittest.mock import patch
  import httpx
  from server import config
  def respond(request):
   is_v3=request.url.path.endswith('/v3/config')
   return httpx.Response(200,json={'code':0,'data':{'models':[{'id':'glm-5.1','maxOutputTokens':4096,'supportsImages':is_v3}]}})
  def client(*args,**kwargs):return httpx.AsyncClient(transport=httpx.MockTransport(respond))
  with patch.object(config,'http_client',side_effect=client):
   ok,models=asyncio.run(tencent.fetch_models({'access_token':'fixture','realm':'cn'}))
  self.assertTrue(ok)
  item=modelcatalog._decorate(models)[0]
  self.assertIsNone(item['supports_images'])
  self.assertTrue(item['image_input_conflict'])
  self.assertEqual(item['image_input_sources'],{'enterprise_models':False,'v3_config':True})
