# coding: utf-8
'''
Use keras models to extract features from images, as specified
by bbdf (output of preproc.py)
'''

from __future__ import division

# import json
import argparse
import codecs
import sys
import ConfigParser
from os.path import isfile

import pandas as pd
import numpy as np

from tqdm import tqdm

from keras.models import Model
from keras import backend

sys.path.append('../Utils')
from utils import print_timestamped_message, code_icorpus, get_image_part
from utils import join_imagenet_id

backend.set_image_data_format('channels_last')


def compute_posfeats(img, bb):
    n_pos_feats = 7
    if bb is None:
        return np.zeros(n_pos_feats)

    ih, iw, _ = img.shape
    x, y, w, h = bb
    # x1, relative
    x1r = x / iw
    # y1, relative
    y1r = y / ih
    # x2, relative
    x2r = (x+w) / iw
    # y2, relative
    y2r = (y+h) / ih
    # area
    area = (w*h) / (iw*ih)
    # ratio image sides (= orientation)
    ratio = iw / ih
    # distance from center (normalised)
    cx = iw / 2
    cy = ih / 2
    bcx = x + w / 2
    bcy = y + h / 2
    distance = np.sqrt((bcx-cx)**2 + (bcy-cy)**2) / np.sqrt(cx**2+cy**2)
    # done!
    return np.array([x1r, y1r, x2r, y2r, area, ratio, distance])


def compute_feats(config, bbdf, model, preproc,
                  xs=224, ys=224, batch_size=100):

    full_image = True if config.get('runtime', 'full_image') == 'True' else False

    filename = config.get('runtime', 'out_dir') +\
        '/%s_%s' % (
            config.get('runtime', 'this_bbdf'),
            config.get('runtime', 'model'))
    if full_image:
        filename += '-fi'
    if isfile(filename + '.npz'):
        print '%s exists. Will not overwrite. ABORTING.' % (filename + '.npz')
        return

    X_pos = []
    X_i = []
    ids = []
    file_counter = 1
    prev_iid, prev_img = (None, None)

    X_out = []

    if full_image:
        bbdf = bbdf.drop_duplicates(subset='image_id')
        bbdf = bbdf.reset_index()

    if 'region_id' in bbdf.columns:
        reg_col = 'region_id'
    if 'object_id' in bbdf.columns:
        reg_col = 'obj_id'

    # FIXME, for debugging only! Reduced size or starting with offset
    # bbdf = bbdf[:1000]

    #  for n, row in tqdm(bbdf.iterrows(), total=len(bbdf)):
    for n, row in bbdf.iterrows():
        this_icorpus = row['i_corpus']
        this_image_id = row['image_id']
        this_region_id = row[reg_col]
        this_bb = row['bb']

        if full_image:
            this_bb = None
            this_region_id = 0

        #  When extracting feats for imagenet regions, must
        #  - create combined filename out of image_id and region_id
        #  - neutralise positional features, by setting bb given
        #    to pos feat computation to 0,0,w,h. So that all ImageNet
        #    regions end up with same positions.
        if code_icorpus[this_icorpus] == 'image_net':
            this_image_id_mod = join_imagenet_id(this_image_id,
                                                 this_region_id)
            this_bb_mod = [0, 0, this_bb[2], this_bb[3]]
        else:
            this_image_id_mod = this_image_id
            this_bb_mod = this_bb

        if this_bb_mod and np.min(this_bb_mod[2:]) <= 0:
            print 'skipping over this image (%s,%d). Negative bb! %s' % \
                (code_icorpus[this_icorpus], this_image_id, str(this_bb_mod))
            continue

        (prev_iid, prev_img), img_resized = \
            get_image_part(config, (prev_iid, prev_img),
                           this_icorpus, this_image_id_mod, this_bb,
                           xs=xs, ys=ys)

        if len(prev_img.shape) != 3 or \
           (len(prev_img.shape) == 3 and prev_img.shape[2] != 3):
            print 'skipping over this image (%s,%d). b/w?' % \
                (code_icorpus[this_icorpus], this_image_id)
            continue

        # If we continue below this line, getting region worked
        X_i.append(img_resized)
        this_pos_feats = compute_posfeats(prev_img, this_bb_mod)
        X_pos.append(this_pos_feats)
        ids.append(np.array([this_icorpus, this_image_id, this_region_id]))

        # is it time to do the actual extraction on this batch
        if (n+1) % batch_size == 0 or n+1 == len(bbdf):
            print_timestamped_message('new batch! (%d %d) Extracting!...' %
                                      (file_counter, n), indent=4)

            try:
                X_i = np.array(X_i)
                # print X_i.shape
                X = model.predict(preproc(X_i.astype('float64')))
            except ValueError:
                print 'Exception! But why? Skipping this whole batch..'
                X_i = []
                ids = []
                X_pos = []
                continue
                # raise e

            X_ids = np.array(ids)
            X_pos = np.array(X_pos)
            print X_ids.shape, X.shape, X_pos.shape
            if full_image:
                X_out.append(np.hstack([X_ids, X]))
            else:
                X_out.append(np.hstack([X_ids, X, X_pos]))

            ids = []
            X_pos = []
            X_i = []
            file_counter += 1
    # and back to the for loop
    X_out = np.concatenate(X_out, axis=0)

    print_timestamped_message('Made it through! Writing out..', indent=4)
    print X_out.shape

    np.savez_compressed(filename, X_out)


# ======== MAIN =========
if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Use keras ConvNets to extract image features')
    parser.add_argument('-c', '--config_file',
                        help='''
                        path to config file specifying data paths.
                        default: '../Config/default.cfg' ''',
                        default='../Config/default.cfg')
    parser.add_argument('-o', '--out_dir',
                        help='''
                        where to put the resulting files.
                        default: './ExtractOut' ''')
    parser.add_argument('-b', '--bbdf_dir',
                        help='''
                        Where to look for the bbdf file.
                        default: '../Preproc/PreProcOut' ''')
    parser.add_argument('-s', '--size_batch',
                        help='''
                        How many images to give to model as one batch.
                        default: 100''',
                        type=int,
                        default=100)
    parser.add_argument('-f', '--full_image',
                        action='store_true',
                        help='Extract whole image, ignore BBs.')
    parser.add_argument('model',
                        choices=['vgg19-fc2', 'rsn50-avg_pool', 'rsn50-flatten_1'],
                        help='''
                        Which model/layer to use for extraction.''')
    parser.add_argument('bbdf',
                        nargs='+',
                        help='''
                        Which bddf(s) to run this on.''')
    args = parser.parse_args()

    config = ConfigParser.SafeConfigParser()

    try:
        with codecs.open(args.config_file, 'r', encoding='utf-8') as f:
            config.readfp(f)
    except IOError:
        print 'no config file found at %s' % (args.config_file)
        sys.exit(1)

    if args.bbdf_dir:
        bbdf_dir = args.bbdf_dir
    elif config.has_option('DSGV-PATHS', 'bbdf_dir'):
        bbdf_dir = config.get('DSGV-PATHS', 'bbdf_dir')
    else:
        bbdf_dir = '../Preproc/PreprocOut'

    if args.out_dir:
        out_dir = args.out_dir
    elif config.has_option('DSGV-PATHS', 'extract_out_dir'):
        out_dir = config.get('DSGV-PATHS', 'extract_out_dir')
    else:
        out_dir = './ExtractOut'

    config.add_section('runtime')
    config.set('runtime', 'out_dir', out_dir)

    print bbdf_dir, out_dir

    config.set('runtime', 'full_image', str(args.full_image))
    print "Full Image Mode Selected! Extraction will take whole image as input."

    # default dimensions
    xs, ys = 224, 224

    arch, layer = args.model.split('-')
    print args.bbdf, arch, layer
    config.set('runtime', 'model', args.model)

    if arch == 'vgg19':
        from keras.applications.vgg19 import VGG19
        from keras.applications.vgg19 import preprocess_input as preproc
        base_model = VGG19(weights='imagenet')
        model = Model(inputs=base_model.input,
                      outputs=base_model.get_layer(layer).output)
    if arch == 'rsn50':
        from keras.applications.resnet50 import ResNet50
        from keras.applications.resnet50 import preprocess_input as preproc
        base_model = ResNet50(weights='imagenet')
        model = Model(inputs=base_model.input,
                      outputs=base_model.get_layer(layer).output)

    print_timestamped_message('starting to extract, using %s %s...' %
                              (arch, layer))

    for this_bbdf in args.bbdf:
        print_timestamped_message('... %s' % (this_bbdf), indent=4)
        this_bbdf_base = bbdf_dir + '/' + this_bbdf + '.json'
        if isfile(this_bbdf_base + '.gz'):
            this_bbdf_path = this_bbdf_base + '.gz'
            bbdf = pd.read_json(this_bbdf_path,
                                orient='split',
                                compression='gzip')
        else:
            this_bbdf_path = this_bbdf_base
            if not isfile(this_bbdf_base):
                print "bbdf file (%s) not found. Aborting." % (this_bbdf_path)
                sys.exit(1)
            bbdf = pd.read_json(this_bbdf_base,
                                orient='split')
        print this_bbdf_path

        config.set('runtime', 'this_bbdf', this_bbdf)

        compute_feats(config, bbdf, model, preproc,
                      xs=xs, ys=ys, batch_size=args.size_batch)
