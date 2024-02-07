from gdl_apps.EMOCA.utils.load import load_model
from gdl.datasets.FaceVideoDataModule import TestFaceVideoDM
import gdl
from pathlib import Path
from tqdm import auto
import argparse
from gdl_apps.EMOCA.utils.io import save_obj, save_images, save_codes, test
import os, shutil, ntpath, glob, re
from pathlib import Path

import time
from datetime import timedelta
import winsound

import tifffile
import imagecodecs

start_time = time.time() # Start time

def process_batch(batch, input_folder, output_folder):
    for file_name in batch:
        try:
            input_path = os.path.join(input_folder, file_name)
            output_path = os.path.join(output_folder, file_name)

            tiff_data = tifffile.imread(input_path)
            tiff_data_normalized = (tiff_data - tiff_data.min()) / (tiff_data.max() - tiff_data.min())
            tiff_data_16bit = (tiff_data_normalized * 65535).astype('uint16')

            tifffile.imwrite(output_path, tiff_data_16bit, dtype='uint16', compression='zlib')

            print(f"Converted and saved {file_name}")
        except Exception as e:
            print(f"Error processing {file_name}: {e}")

def batch_process_files(input_folder, output_folder, batch_size):
    input_files = [file_name for file_name in os.listdir(input_folder)
                   if file_name.lower().endswith((".tif", ".tiff"))]

    for i in range(0, len(input_files), batch_size):
        batch = input_files[i:i + batch_size]
        process_batch(batch, input_folder, output_folder)

def str2bool(v):
    if isinstance(v, bool):
        return v
    if v.lower() in ('yes', 'true', 't', 'y', '1'):
        return True
    elif v.lower() in ('no', 'false', 'f', 'n', '0'):
        return False
    else:
        raise argparse.ArgumentTypeError('Boolean value expected.')


def reconstruct_video(args):
    path_to_models = args.path_to_models
    input_video = args.input_video
    model_name = args.model_name
    tmp_output_folder = "emoca_temp"
    outputPath = args.tmp_output_folder

    mode = args.mode
   
    ## 1) Process the video - extract the frames from video and run face detection
    dm = TestFaceVideoDM(input_video, 
        tmp_output_folder, 
        processed_subfolder=None, 
        batch_size=60, 
        num_workers=0)
    dm.prepare_data()
    dm.setup()
    processed_subfolder = Path(dm.output_dir).name

    # ## 2) Load the model
    emoca, conf = load_model(path_to_models, model_name, mode)
    emoca.cuda()
    emoca.eval()

    if Path(tmp_output_folder).is_absolute():
        outfolder = tmp_output_folder
    else:
        outfolder = str(Path(tmp_output_folder) / processed_subfolder / Path(input_video).stem / "results" / model_name)

    ## 3) Get the data loader with the detected faces
    dl = dm.test_dataloader()

    ## 4) Run the model on the data
    print("Running model on the data.")
    for j, batch in enumerate (auto.tqdm(dl)):
        current_bs = batch["image"].shape[0]
        img = batch
        vals, visdict = test(emoca, img)
        #print("vals: " + str(vals))  # Convert vals to a string using str() function

        for i in range(current_bs):
            name =  batch["image_name"][i]
            sample_tmp_output_folder = Path(outfolder) /name
            sample_tmp_output_folder.mkdir(parents=True, exist_ok=True)
            save_images(outfolder, name, visdict, i)

    ## 5) Create the reconstruction video
    outFileSpec = dm.create_reconstruction_video(0,  
            rec_method=model_name, 
            image_type="geometry_detail",
            overwrite=True, 
            cat_dim=0, 
            include_transparent=False, 
            include_original=False, 
            include_rec = True,
            black_background=True, 
            use_mask=False, 
            out_folder=outfolder)

    # Calculate path of final output
    baseMediaName = str(Path(input_video).stem)
    destPath = Path(os.path.join(outputPath, baseMediaName)).absolute()
    # Make sure destination path exists
    try:
        #os.makedirs(Path(destPath).parent.absolute())
        os.makedirs(destPath)
    except:
        pass

    # Get list of output files
    fileList = glob.glob(outFileSpec)

    # Iterate over the file list and copy each file to the destination directory, changing the base name
    for file in fileList:
        origFilename = str(Path(file).stem) + ".tif"
        newFilename = re.sub(re.sub(r"_face\d\.\d\d\d\d\.tif", "", origFilename, flags=re.IGNORECASE), baseMediaName, origFilename, flags=re.IGNORECASE)
        newFilePath = os.path.join(destPath, newFilename)
        shutil.copy(file, newFilePath)

    print("Exported TIF image sequence(s) to " + str(destPath))

    # Kill off temporary export folder
    deleteFolder = Path(tmp_output_folder).absolute()
    print("Killing off " + str(deleteFolder) + "...")
    shutil.rmtree(deleteFolder)
    print("Done")

    #Eric Added to Convert 32-bit to 16-bit
    input_folder = destPath
    output_folder = input_folder
    batch_size = 50
    batch_process_files(input_folder, output_folder, batch_size)

    end_time = time.time() # End time
    elapsed_time = end_time - start_time # Calculate the elapsed time
    formatted_time = str(timedelta(seconds=elapsed_time)).split(".")[0] # Format the elapsed time
    print()
    print(f"Elapsed time: {formatted_time}") # Print the elapsed time
    #winsound.Beep(1000, 500)

def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('--input_video', type=str, default=str(Path(gdl.__file__).parents[1] / "/assets/data/EMOCA_test_example_data/videos/82-25-854x480_affwild2.mp4"), 
        help="Filename of the video for reconstruction.")
    parser.add_argument('--tmp_output_folder', type=str, default="emocaOutput", help="Output folder to save the result to.")
    parser.add_argument('--model_name', type=str, default='EMOCA_v2_lr_mse_20', help='Name of the model to use. Currently EMOCA or DECA are available.')
    parser.add_argument('--path_to_models', type=str, default=str(Path(gdl.__file__).parents[1] / "assets/EMOCA/models"))
    parser.add_argument('--mode', type=str, default="detail", choices=["detail", "coarse"], help="Which model to use for the reconstruction.")

    args = parser.parse_args()
    return args


def main():
    args = parse_args()
    reconstruct_video(args)
    #print("Done")

if __name__ == '__main__':
    main()
