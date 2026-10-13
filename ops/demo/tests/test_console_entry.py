import hashlib,sys,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import promote_console as p
class ConsoleEntryTests(unittest.TestCase):
 def test_only_exact_entry_changes(self):
  base=('before\n'+p.OLD+'\nafter /demo/* reverse_proxy /acme').encode()
  new=p.render(base,hashlib.sha256(base).hexdigest())
  self.assertEqual(new,base.replace(p.OLD.encode(),p.NEW.encode(),1))
  self.assertIn(b'path /demo\n',new);self.assertIn(b'/console 302',new)
 def test_wrong_sha_rejected(self):
  with self.assertRaises(ValueError):p.render(p.OLD.encode(),'0'*64)
 def test_missing_or_duplicate_entry_rejected(self):
  for base in [b'other',(p.OLD+p.OLD).encode()]:
   with self.assertRaises(ValueError):p.render(base,hashlib.sha256(base).hexdigest())
 def test_acceptance_failure_restores_alias(self):
  calls=[]
  with patch.object(p,'link',side_effect=lambda a,b:calls.append(a)),patch.object(p,'verify'):
   with self.assertRaises(RuntimeError):p.switch('old','new',lambda:None,lambda:(_ for _ in ()).throw(RuntimeError()))
  self.assertEqual(calls,['new','old'])
if __name__=='__main__':unittest.main()
