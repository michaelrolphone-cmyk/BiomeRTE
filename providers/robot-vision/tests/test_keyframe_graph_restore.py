import ast,collections,pathlib,types,unittest,sys
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]));from setup_slam import patch_keyframe_graph_restore
METHOD='''def replace_ids_with_objects(self, points, frames, keyframes):
        keyframes_dict = {obj.id: obj for obj in keyframes if obj is not None}
        def get_object_with_id(id, lookup_dict):
            return lookup_dict.get(id, None)
        if self.parent is not None:
            self.parent = get_object_with_id(self.parent, keyframes_dict)
        if self.children is not None:
            self.children = {get_object_with_id(id, keyframes_dict) for id in self.children}
        if self.loop_edges is not None:
            self.loop_edges = {get_object_with_id(id, keyframes_dict) for id in self.loop_edges}
        if self.connected_keyframes_weights is not None:
            self.connected_keyframes_weights = Counter(
                {
                    get_object_with_id(id, keyframes_dict): weight
                    for id, weight in self.connected_keyframes_weights
                }
            )
        if self.ordered_keyframes_weights is not None:
            self.ordered_keyframes_weights = OrderedDict(
                {
                    get_object_with_id(id, keyframes_dict): weight
                    for id, weight in self.ordered_keyframes_weights
                }
            )
'''
class GraphRestore(unittest.TestCase):
 def test_missing_culled_edges_do_not_become_none_neighbors(self):
  scope={'Counter':collections.Counter,'OrderedDict':collections.OrderedDict};exec(patch_keyframe_graph_restore(METHOD),scope)
  class KF:
   def __init__(self,i):self.id=i
  k1,k2=KF(1),KF(2);g=types.SimpleNamespace(parent=9,children={1,9},loop_edges={2,9},connected_keyframes_weights=[(1,8),(9,12)],ordered_keyframes_weights=[(9,12),(2,5),(1,3)])
  scope['replace_ids_with_objects'](g,[],[],[k1,None,k2]);self.assertIsNone(g.parent);self.assertEqual(g.children,{k1});self.assertEqual(g.loop_edges,{k2});self.assertEqual(dict(g.connected_keyframes_weights),{k1:8});self.assertEqual(list(g.ordered_keyframes_weights.items()),[(k2,5),(k1,3)])
 def test_unknown_upstream_shape_is_rejected(self):
  with self.assertRaises(RuntimeError):patch_keyframe_graph_restore('def changed(): pass')
if __name__=='__main__':unittest.main()
