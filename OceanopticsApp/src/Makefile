# SPDX-FileCopyrightText: 1998 Argonne National Laboratory
#
# SPDX-License-Identifier: EPICS

TOP=../..

include $(TOP)/configure/CONFIG
include $(PYDEVICE)/configure/CONFIG.PyDevice
#----------------------------------------
#  ADD MACRO DEFINITIONS BELOW HERE

# Use typed rset structure (see 3.16.1 release notes)
USR_CPPFLAGS += -DUSE_TYPED_RSET
# Point at the lib directory of the Python used by PyDevice.
# Override in configure/CONFIG_SITE.local if needed, e.g.:
#   USR_LDFLAGS += -L/opt/conda/envs/myenv/lib
ifdef PYTHON_LIB_DIR
USR_LDFLAGS += -L$(PYTHON_LIB_DIR)
endif

# xxxRecord.h will be created from xxxRecord.dbd
DBDINC += xxxRecord

# Install xxxSupport.dbd into <top>/dbd
DBD += xxxSupport.dbd

# Build an IOC support library
LIBRARY_IOC += OceanopticsSupport

# Compile and add code to the support library
OceanopticsSupport_SRCS += xxxRecord.c
OceanopticsSupport_SRCS += devXxxSoft.c

# Link locally-provided code into the support library,
# rather than directly into the IOC application, that
# causes problems on Windows DLL builds
OceanopticsSupport_SRCS += dbSubExample.c
OceanopticsSupport_SRCS += devOceanopticsVersion.c
OceanopticsSupport_SRCS += OceanopticsHello.c
OceanopticsSupport_SRCS += initTrace.c

OceanopticsSupport_LIBS += $(EPICS_BASE_IOC_LIBS)

# Auto-generate a header file containing a version string.
# Version comes from the VCS if available, else date+time.
GENVERSION = OceanopticsVersion.h
# Macro name
GENVERSIONMACRO = OceanopticsVERSION

# Build the IOC application
PROD_IOC = Oceanoptics

# Oceanoptics.dbd will be created and installed
DBD += Oceanoptics.dbd

# Oceanoptics.dbd will include these files:
Oceanoptics_DBD += base.dbd
Oceanoptics_DBD += xxxSupport.dbd
Oceanoptics_DBD += dbSubExample.dbd
Oceanoptics_DBD += devOceanopticsVersion.dbd
Oceanoptics_DBD += OceanopticsHello.dbd
Oceanoptics_DBD += initTrace.dbd
Oceanoptics_DBD += mca.dbd
Oceanoptics_DBD += pydev.dbd

# Oceanoptics_registerRecordDeviceDriver.cpp derives from Oceanoptics.dbd
Oceanoptics_SRCS += Oceanoptics_registerRecordDeviceDriver.cpp

# Build the main IOC entry point where needed
Oceanoptics_SRCS_DEFAULT += OceanopticsMain.cpp
Oceanoptics_SRCS_vxWorks += -nil-

# Link in the code from our support library
Oceanoptics_LIBS += OceanopticsSupport
Oceanoptics_LIBS += mca
Oceanoptics_LIBS += asyn
Oceanoptics_LIBS += pydev

# To build SNL programs, SNCSEQ must be defined
# in the <top>/configure/RELEASE file
ifneq ($(SNCSEQ),)
    # Build sncExample into OceanopticsSupport
    sncExample_SNCFLAGS += +r
    Oceanoptics_DBD += sncExample.dbd
    # A .stt sequence program is *not* pre-processed:
    OceanopticsSupport_SRCS += sncExample.stt
    OceanopticsSupport_LIBS += seq pv
    Oceanoptics_LIBS += seq pv

    # Build sncProgram as a standalone program
    PROD_HOST += sncProgram
    sncProgram_SNCFLAGS += +m
    # A .st sequence program *is* pre-processed:
    sncProgram_SRCS += sncProgram.st
    sncProgram_LIBS += seq pv
    sncProgram_LIBS += $(EPICS_BASE_HOST_LIBS)
endif

# Link QSRV (pvAccess Server) if available
ifdef EPICS_QSRV_MAJOR_VERSION
    Oceanoptics_LIBS += qsrv
    Oceanoptics_LIBS += $(EPICS_BASE_PVA_CORE_LIBS)
    Oceanoptics_DBD += PVAServerRegister.dbd
    Oceanoptics_DBD += qsrv.dbd
endif

# Finally link IOC to the EPICS Base libraries
Oceanoptics_LIBS += $(EPICS_BASE_IOC_LIBS)

include $(TOP)/configure/RULES
#----------------------------------------
#  ADD EXTRA GNUMAKE RULES BELOW HERE

# Explicit dependency needed for generated header file
devOceanopticsVersion$(DEP): $(COMMON_DIR)/$(GENVERSION)
