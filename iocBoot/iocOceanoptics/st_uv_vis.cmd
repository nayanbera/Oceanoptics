#!../../bin/linux-x86_64/Oceanoptics

#- SPDX-FileCopyrightText: 2000 Argonne National Laboratory
#-
#- SPDX-License-Identifier: EPICS

#- You may have to change Oceanoptics to something else
#- everywhere it appears in this file

< envPaths

# PYTHONPATH points to folders where Python modules are.
epicsEnvSet("PYTHONPATH","$(TOP)/python")

# Setting the EPICS IOC shell prompt
epicsEnvSet("IOCSH_PS1","iocUVVis>")

cd "${TOP}"


## Register all support components
dbLoadDatabase "dbd/Oceanoptics.dbd"
Oceanoptics_registerRecordDeviceDriver pdbbase

## Soft MCA port -- requires drvSoftMca.dbd to be compiled into the IOC.
## Uncomment these two lines once drvSoftMca is available in your mca build:
#drvSoftMcaConfigure("QEPRO_MCA", 1044)

## autosave / save_restore -- preserve PV values across IOC restarts
epicsEnvSet("SAVE_DIR","$(TOP)/autosave")
system("mkdir -p $(TOP)/autosave")
set_savefile_path("$(TOP)/autosave")
set_requestfile_path("$(TOP)/OceanopticsApp/Db")
save_restoreSet_NumSeqFiles(3)
save_restoreSet_SeqPeriodInSeconds(600)
set_pass0_restoreFile("auto_settings.sav")
set_pass1_restoreFile("auto_settings.sav")

## Load record instances
#dbLoadTemplate "db/user.substitutions"
dbLoadRecords "db/OceanopticsVersion.db", "user=chem_epics"
#dbLoadRecords "db/dbSubExample.db", "user=chem_epics"
dbLoadRecords "db/OceanopticsPV.db",        "P=15ID,D=UVVis"
#dbLoadRecords "db/OceanopticsMCA.db",      "P=15ID,D=UVVis"  # requires drvSoftMca port
dbLoadRecords "db/OceanopticsMCA_pydev.db", "P=15ID,D=UVVis"  # MCA-compatible, no drvSoftMca needed
dbLoadRecords "db/OceanopticsQEPro.db",    "P=15ID,D=UVVis"
dbLoadRecords "db/OceanopticsHDF5.db",     "P=15ID,D=UVVis"



#- Set this to see messages from mySub
#-var mySubDebug 1

#- Run this to trace the stages of iocInit
#-traceIocInit

cd "${TOP}/iocBoot/${IOC}"

pydev("import warnings")
pydev("warnings.filterwarnings('ignore')")
pydev("from pyOceanOptics import *")
# Tell the MCA bridge which PV prefix to use for loopback CA push
pydev("ioc_prefix = '15ID:UVVis:'")
# Load the default NeXus XML layout from the iocBoot directory.
# Working directory is ${TOP} at this point.
pydev("hdf_set_xml_filename('iocBoot/iocOceanoptics/hdf5_layout.xml')")

iocInit

## Start autosave periodic saves (every 30 s, macro P=15ID D=UVVis)
create_monitor_set("auto_settings.req", 30, "P=15ID,D=UVVis")

## Start any sequence programs
#seq sncExample, "user=chem_epics"
