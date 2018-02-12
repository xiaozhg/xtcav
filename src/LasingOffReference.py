
import os
import time
import psana
import numpy as np
import glob
import pdb
import IPython
import sys
import getopt
import warnings
import Utils as xtu
import UtilsPsana as xtup
from DarkBackground import *
from LasingOffReference import *
from CalibrationPaths import *
from FileInterface import Load as constLoad
from FileInterface import Save as constSave

# PP imports
from mpi4py import MPI
comm = MPI.COMM_WORLD
rank = comm.Get_rank()
size = comm.Get_size()
#print 'Core %s ... ready' % (rank + 1) # useful for debugging purposes
#sys.stdout.flush()
"""
    Cladd that generates a set of lasing off references for XTCAV reconstruction purposes
    Attributes:
        experiment (str): String with the experiment reference to use. E.g. 'amoc8114'
        runs (str): String with a run number, or a run interval. E.g. '123'  '134-156' 145,136'
        maxshots (int): Maximum number of images to use for the references.
        calibrationpath (str): Custom calibration directory in case the default is not intended to be used.
        nb (int): Number of bunches.
        medianfilter (int): Number of neighbours for median filter.
        snrfilter (float): Number of sigmas for the noise threshold.
        groupsize (int): Number of profiles to average together for each reference.
        roiwaistthres (float): ratio with respect to the maximum to decide on the waist of the XTCAV trace.
        roiexpand (float): number of waists that the region of interest around will span around the center of the trace.
        islandsplitmethod (str): island splitting algorithm. Set to 'scipylabel' or 'contourLabel'  The defaults parameter is 'scipylabel'.
"""

class LasingOffReference(object):
    averagedProfiles=[]
    run=''
    n=0
    parameters=None


    def __init__(self, 
            experiment='amoc8114', #Experiment label
            maxshots=401,  #Maximum number of valid shots to process
            run_number='86',       #Run number
            validityrange=None,
            darkreferencepath=None, #Dark reference information
            nb=1,                   #Number of bunches
            groupsize=5 ,           #Number of profiles to average together
            medianfilter=3,         #Number of neighbours for median filter
            snrfilter=10,           #Number of sigmas for the noise threshold
            roiwaistthres=0.2,      #Parameter for the roi location
            roiexpand=2.5,          #Parameter for the roi location
            islandsplitmethod = 'scipyLabel',      #Method for island splitting
            islandsplitpar1 = 3.0,  #Ratio between number of pixels between largest and second largest groups when calling scipy.label
            islandsplitpar2 = 5.,   #Ratio between number of pixels between second/third largest groups when calling scipy.label
            calpath=''):

        self.parameters = Parameters(experiment = experiment,
            maxshots = maxshots, run = run_number, validityrange = validityrange, 
            darkreferencepath = darkreferencepath, nb=nb, groupsize=groupsize, 
            medianfilter=medianfilter, snrfilter=snrfilter, roiwaistthres=roiwaistthres,
            roiexpand = roiexpand, islandsplitmethod=islandsplitmethod, islandsplitpar2 = islandsplitpar2,
            islandsplitpar1=islandsplitpar1, calpath=calpath, version=1)


    def Generate(self, savetofile=True):
        
        #Handle warnings
        warnings.filterwarnings('always',module='Utils',category=UserWarning)
        warnings.filterwarnings('ignore',module='Utils',category=RuntimeWarning, message="invalid value encountered in divide")

        print 'Lasing off reference'
        print '\t Experiment: %s' % self.parameters.experiment
        print '\t Runs: %s' % self.parameters.run
        print '\t Number of bunches: %d' % self.parameters.nb
        print '\t Valid shots to process: %d' % self.parameters.maxshots
        print '\t Dark reference run: %s' % self.parameters.darkreferencepath
        
        #Loading the data, this way of working should be compatible with both xtc and hdf5 files
        dataSource=psana.DataSource("exp=%s:run=%s:idx" % (self.parameters.experiment, self.parameters.run))

        #Camera for the xtcav images
        xtcav_camera = psana.Detector('XrayTransportDiagnostic.0:Opal1000.0')

        #Ebeam type: it should actually be the version 5 which is the one that contains xtcav stuff
        ebeam_data = psana.Detector('EBeam')

        #Gas detectors for the pulse energies
        gasdetector_data = psana.Detector('FEEGasDetEnergy')

        #Stores for environment variables   
        epicsStore = dataSource.env().epicsStore()

        #Empty lists for the statistics obtained from each image, the shot to shot properties, and the ROI of each image (although this ROI is initially the same for each shot, it becomes different when the image is cropped around the trace)
        listImageStats=[]
        listShotToShot=[]
        listROI=[]
        listPU=[]
            
        run=dataSource.runs().next()

        ROI_XTCAV, last_image = xtup.GetXTCAVImageROI(epicsStore, run, xtcav_camera)
        global_calibration, last_image = xtup.GetGlobalXTCAVCalibration(epicsStore, run, xtcav_camera, start=last_image)
        saturation_value = xtup.GetCameraSaturationValue(epicsStore, run, xtcav_camera, start=last_image)

        if not self.parameters.darkreferencepath:
            cp = CalibrationPaths(dataSource.env(), self.parameters.calpath)
            darkreferencepath = cp.findCalFileName('pedestals', int(self.parameters.run))
            self.parameters = self.parameters._replace(darkreferencepath = darkreferencepath)
        
        if not self.parameters.darkreferencepath:
            print ('Dark reference for run %s not found, image will not be background substracted' % self.parameters.run)
            dark_background = None
        else:
            dark_background = DarkBackground.Load(self.parameters.darkreferencepath)


        num_processed = 0 #Counter for the total number of xtcav images processed within the run        
        times = run.times()

        #  Parallel Processing implementation by andr0s and polo5
        #  The run will be segmented into chunks of 4 shots, with each core alternatingly assigned to each.
        #  e.g. Core 1 | Core 2 | Core 3 | Core 1 | Core 2 | Core 3 | ....
        image_numbers = xtup.DivideImageTasks(last_image + 1, rank, size)

        for t in image_numbers[::-1]: #  Starting from the back, to avoid waits in the cases where there are not xtcav images for the first shots
            evt=run.event(times[int(t)])

            #ignore shots without xtcav, because we can get incorrect EPICS information (e.g. ROI).  this is
            #a workaround for the fact that xtcav only records epics on shots where it has camera data, as well
            #as an incorrect design in psana where epics information is not stored per-shot (it is in a more global object
            #called "Env")
            img = xtcav_camera.image(evt)
            # skip if empty image or saturated
            if img is None: 
                continue

            if np.max(img) >= saturation_value:
                warnings.warn_explicit('Saturated Image',UserWarning,'XTCAV',0)
                continue

            ebeam = ebeam_data.get(evt)
            gasdetector = gasdetector_data.get(evt)

            shot_to_shot = xtup.GetShotToShotParameters(ebeam, gasdetector, evt.get(psana.EventId)) #Obtain the shot to shot parameters necessary for the retrieval of the x and y axis in time and energy units
            if not shot_to_shot.valid: #If the information is not good, we skip the event
                continue
            
            #Subtract the dark background, taking into account properly possible different ROIs, if it is available
            img, ROI = xtu.SubtractBackground(img, ROI_XTCAV, dark_background)         
            img, contains_data = xtu.DenoiseImage(img, self.parameters.medianfilter, self.parameters.snrfilter)                    #Remove noise from the image and normalize it
            if not contains_data:                                        #If there is nothing in the image we skip the event  
                continue

            img, ROI=xtu.FindROI(img, ROI, self.parameters.roiwaistthres, self.parameters.roiexpand)                  #Crop the image, the ROI struct is changed. It also add an extra dimension to the image so the array can store multiple images corresponding to different bunches
            if ROI.xN < 3 or ROI.yN < 3:
                print 'ROI too small', ROI.xN, ROI.yN
                continue

            img = xtu.SplitImage(img, self.parameters.nb, self.parameters.islandsplitmethod, self.parameters.islandsplitpar1, self.parameters.islandsplitpar2)#new

            if self.parameters.nb!=img.shape[0]:
                continue

            image_stats = xtu.ProcessXTCAVImage(img,ROI)          #Obtain the different properties and profiles from the trace               

            physical_units = xtu.CalculatePhyscialUnits(ROI,[image_stats[0].xCOM,image_stats[0].yCOM],shot_to_shot,global_calibration)   
            if not physical_units.valid:
                continue

            #If the step in time is negative, we mirror the x axis to make it ascending and consequently mirror the profiles
            if physical_units.xfsPerPix < 0:
                physical_units.xfs=physical_units.xfs[::-1]
                NB=len(imageStats)
                for j in range(NB):
                    image_stats[j].xProfile=image_stats[j].xProfile[::-1]
                    image_stats[j].yCOMslice=image_stats[j].yCOMslice[::-1]
                    image_stats[j].yRMSslice=image_stats[j].yRMSslice[::-1]                                               
                                                                                                                                                                                    
            listImageStats.append(image_stats)
            listShotToShot.append(shot_to_shot)
            listROI.append(ROI)
            listPU.append(physical_units)
            
            num_processed += 1
            # print core numb and percentage

            if num_processed % 5 == 0:
                if size==1:extrainfo='\r'
                else:extrainfo='\nCore %d: '%(rank + 1)
                sys.stdout.write('%s%.1f %% done, %d / %d' % (extrainfo, float(num_processed) / np.ceil(self.parameters.maxshots/float(size)) *100, num_processed, np.ceil(self.parameters.maxshots/float(size))))
                sys.stdout.flush()
            if num_processed >= np.ceil(self.parameters.maxshots/float(size)):
                sys.stdout.write('\n')
                break

        #  here gather all shots in one core, add all lists
        exp = {'listImageStats': listImageStats, 'listShotToShot': listShotToShot, 'listROI': listROI, 'listPU': listPU}
        processedlist = comm.gather(exp, root=0)
        
        if rank != 0:
            return
        
        listImageStats = []
        listShotToShot = []
        listROI = []
        listPU = []
        
        for i in range(size):
            p = processedlist[i]
            listImageStats += p['listImageStats']
            listShotToShot += p['listShotToShot']
            listROI += p['listROI']
            listPU += p['listPU']
            
        #Since there are 12 cores it is possible that there are more references than needed. In that case we discard some
        if len(listImageStats) > self.parameters.maxshots:
            listImageStats=listImageStats[0:self.parameters.maxshots]
            listShotToShot=listShotToShot[0:self.parameters.maxshots]
            listROI=listROI[0:self.parameters.maxshots]
            listPU=listPU[0:self.parameters.maxshots]
            
        #At the end, all the reference profiles are converted to Physical units, grouped and averaged together
        averagedProfiles = xtu.AverageXTCAVProfilesGroups(listROI,listImageStats,listShotToShot,listPU,self.parameters.groupsize);     

        self.averagedProfiles=averagedProfiles
        self.n=num_processed    
        
        if not self.parameters.validityrange:
            self.parameters.validityrange=[runs[0], 'end']
            
        cp=CalibrationPaths(dataSource.env(),self.parameters.calpath)
        file=cp.newCalFileName('lasingoffreference',self.parameters.validityrange[0],self.parameters.validityrange[1])
           
        if savetofile:
            self.Save(file)

        
    def Save(self,path):
        # super hacky... allows us to save without overwriting current instance
        instance = copy.deepcopy(self)
        if instance.parameters:
            instance.parameters = dict(instance.parameters._asdict())     
        constSave(instance,path)

    @staticmethod    
    def Load(path):        
        return constLoad(path)
