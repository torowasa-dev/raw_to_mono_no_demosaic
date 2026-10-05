"""Run: python -m unittest -v test_converter.py"""
import contextlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import numpy as np
import tifffile
import rawpy
import raw_to_mono_no_demosaic as mono

class ConverterTests(unittest.TestCase):
    def setUp(self):
        self.raw = SimpleNamespace(
            raw_image_visible=np.array([[100,300,200,600,999],[500,700,400,800,999],
                [200,400,500,900,999],[600,800,700,1000,999],[999]*5], dtype=np.uint16),
            raw_pattern=np.array([[0,1],[3,2]]), color_desc=b'RGBG',
            raw_colors_visible=np.tile([[0,1],[3,2]], (3,3))[:5,:5],
            black_level_per_channel=[100]*4, white_level=1100,
            camera_white_level_per_channel=None, camera_whitebalance=[2,1,1.5,1])
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
    def convert(self, name, **kwargs):
        p = self.base/name
        with patch.object(mono.rawpy, 'imread', return_value=contextlib.nullcontext(self.raw)):
            mono.convert(self.base/'input.raw', p, **kwargs)
        return p
    def test_mean_and_legacy_linear(self):
        p=self.convert('linear.tif', gamma=1, auto_exposure=False)
        expected=np.rint(np.array([[.3,.4],[.4,.675]])*65535)
        np.testing.assert_allclose(tifffile.imread(p), expected, atol=1)
        p=self.convert('codes.tif', mode='codes')
        np.testing.assert_array_equal(tifffile.imread(p), [[400,500],[500,775]])
    def test_target_and_highlights(self):
        values=np.linspace(.001,.999,10001)**3
        ev=mono.auto_ev(values,.5,2,soft=True)
        rendered=mono.render_tone(values,ev,2,soft=True)
        self.assertAlmostEqual(float(rendered.mean()),.5,places=4)
        self.assertTrue(np.all(np.diff(rendered)>0))
        self.assertTrue(np.all(rendered<1))
        np.testing.assert_array_equal(mono.render_tone(np.array([0.,1.]),10,2,soft=True),[0,1])
        self.assertEqual(mono.auto_ev(np.zeros(10),.5,2,soft=True),0)
    def test_dng_pixels_and_tags(self):
        expected=np.rint(np.array([[.3,.4],[.4,.675]])*65535)
        for codec in ('lossless','none'):
            p=self.convert(codec+'.dng', compression=codec)
            with tifffile.TiffFile(p) as t:
                page=t.pages[0]
                self.assertEqual(page.photometric,34892)
                self.assertEqual(page.samplesperpixel,1)
                self.assertNotIn(33422,page.tags)
                self.assertIn(50730,page.tags)
                np.testing.assert_allclose(page.asarray(),expected,atol=1)
        a=self.convert('fixed.dng', auto_exposure=False, exposure=3)
        b=self.convert('auto.dng')
        np.testing.assert_array_equal(tifffile.imread(a),tifffile.imread(b))
    def test_multi_tile_dng_independent_decoder(self):
        pixels=np.random.default_rng(7).integers(0,65536,(513,517),dtype=np.uint16)
        for codec in ('lossless','none'):
            p=self.base/(codec+'_tiles.dng')
            mono.write_image(p,pixels,dng=True,ev=2,description='test',compression=codec,force=False)
            with rawpy.imread(str(p)) as raw:
                self.assertEqual(raw.num_colors,1)
                np.testing.assert_array_equal(raw.raw_image_visible,pixels)
    def test_protection_and_invalid_arguments(self):
        p=self.convert('keep.tif');before=p.read_bytes()
        with self.assertRaises(FileExistsError):self.convert('keep.tif')
        self.assertEqual(p.read_bytes(),before)
        for kwargs in ({'gamma':float('nan')},{'target_level':0},
                       {'mode':'codes','exposure':1},{'compression':'jpeg'}):
            with self.assertRaises(ValueError):self.convert('bad.tif',**kwargs)
        with self.assertRaises(ValueError):self.convert('bad.dng',gamma=2)
        self.raw.raw_pattern=np.zeros((6,6),dtype=int)
        with self.assertRaises(ValueError):self.convert('xtrans.tif')
        self.assertFalse((self.base/'xtrans.tif').exists())

if __name__=='__main__':unittest.main()
