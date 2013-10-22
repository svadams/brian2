'''
Module implementing the C++ "standalone" device.
'''
import numpy
import os
import subprocess
import inspect
from collections import defaultdict
import weakref

from brian2.core.clocks import defaultclock
from brian2.core.network import Network as OrigNetwork
from brian2.core.namespace import get_local_namespace
from brian2.devices.device import Device, all_devices
from brian2.core.preferences import brian_prefs
from brian2.core.variables import *
from brian2.synapses.synapses import Synapses as OrigSynapses
from brian2.utils.filetools import copy_directory, ensure_directory, in_directory
from brian2.utils.stringtools import word_substitute
from brian2.codegen.languages.cpp_lang import c_data_type
from brian2.codegen.codeobject import CodeObjectUpdater
from brian2.units.fundamentalunits import (Quantity, Unit, is_scalar_type,
                                           fail_for_dimension_mismatch,
                                           have_same_dimensions,
                                           )
from brian2.units import second

from .codeobject import CPPStandaloneCodeObject


__all__ = ['build', 'Network', 'run', 'reinit', 'stop',
           'Synapses',
           ]

def freeze(code, ns):
    # this is a bit of a hack, it should be passed to the template somehow
    for k, v in ns.items():
        if isinstance(v, (int, float)):
            code = word_substitute(code, {k: repr(v)})
    return code

class StandaloneVariableView(VariableView):
    '''
    Will store information about how the variable was set in the original
    `ArrayVariable` object.
    '''
    def __init__(self, name, variable, group, unit=None, level=0):
        super(StandaloneVariableView, self).__init__(name, variable, group,
                                                     unit=unit, level=level)

    # Overwrite methods to signal that they are not available for standalone
    def set_array_with_array_index(self, item, value):
        raise NotImplementedError(('Cannot set variables this way in'
                                   'standalone, try using string expressions.'))

    def __getitem__(self, item):
        raise NotImplementedError()


class StandaloneArrayVariable(ArrayVariable):

    def __init__(self, name, unit, size, dtype, group_name=None, constant=False,
                 is_bool=False):
        self.assignments = []
        self.size = size
        super(StandaloneArrayVariable, self).__init__(name, unit, value=None,
                                                      group_name=group_name,
                                                      constant=constant,
                                                      is_bool=is_bool)
        self.dtype = dtype

    def get_len(self):
        return self.size

    def get_value(self):
        raise NotImplementedError()

    def set_value(self, value, index=None):
        if index is None:
            index = slice(None)
        self.assignments.append((index, value))

    def get_addressable_value(self, name, group, level=0):
        return StandaloneVariableView(name, self, group, unit=None,
                                      level=level+1)

    def get_addressable_value_with_unit(self, name, group, level=0):
        return StandaloneVariableView(name, self, group, unit=self.unit,
                                      level=level+1)


class StandaloneDynamicArrayVariable(StandaloneArrayVariable):

    def resize(self, new_size):
        self.assignments.append(('resize', new_size))


class CPPStandaloneDevice(Device):
    '''
    '''
    def __init__(self):
        #: List of all regular arrays with their type and size
        self.array_specs = []
        #: List of all dynamic arrays with their type
        self.dynamic_array_specs = []
        #: List of all arrays to be filled with zeros (with type and size)
        self.zero_specs = []
        #: List of all arrays to be filled with numbers (with type, size,
        #: start, and stop)
        self.arange_specs = []
        self.code_objects = {}
        self.main_queue = []
        
        self.synapses = []
        
    def array(self, owner, name, size, unit, dtype=None, constant=False,
              is_bool=False, read_only=False):
        if is_bool:
            dtype = numpy.bool
        elif dtype is None:
            dtype = brian_prefs['core.default_scalar_dtype']
        self.array_specs.append(('_array_%s_%s' % (owner.name, name),
                                 c_data_type(dtype), size))
        self.zero_specs.append(('_array_%s_%s' % (owner.name, name),
                                c_data_type(dtype), size))
        return StandaloneArrayVariable(name, unit, size=size, dtype=dtype,
                                       group_name=owner.name,
                                       constant=constant, is_bool=is_bool)

    def arange(self, owner, name, size, start=0, dtype=numpy.int32, constant=True,
               read_only=True):
        self.array_specs.append(('_array_%s_%s' % (owner.name, name),
                                 c_data_type(dtype), size))
        self.arange_specs.append(('_array_%s_%s' % (owner.name, name),
                                  c_data_type(dtype), start, size))
        return StandaloneArrayVariable(name, Unit(1), size=size, dtype=dtype,
                                       group_name=owner.name,
                                       constant=constant, is_bool=False)

    def dynamic_array_1d(self, owner, name, size, unit, dtype=None,
                         constant=False, constant_size=True, is_bool=False,
                         read_only=False):
        if is_bool:
            dtype = numpy.bool
        elif dtype is None:
            dtype = brian_prefs['core.default_scalar_dtype']
        self.dynamic_array_specs.append(('_dynamic_array_%s_%s' % (owner.name, name),
                                         c_data_type(dtype)))
        return StandaloneDynamicArrayVariable(name, unit, size=size,
                                              dtype=dtype,
                                              group_name=owner.name,
                                              constant=constant, is_bool=is_bool)

    def code_object_class(self, codeobj_class=None):
        if codeobj_class is not None:
            raise ValueError("Cannot specify codeobj_class for C++ standalone device.")
        return CPPStandaloneCodeObject

    def code_object(self, owner, name, abstract_code, namespace, variables, template_name,
                    variable_indices, codeobj_class=None, template_kwds=None):
        codeobj = super(CPPStandaloneDevice, self).code_object(owner, name, abstract_code, namespace, variables,
                                                               template_name, variable_indices,
                                                               codeobj_class=codeobj_class,
                                                               template_kwds=template_kwds,
                                                               )
        self.code_objects[codeobj.name] = codeobj
        return codeobj

    def build(self, project_dir='output', compile_project=True, run_project=False, debug=True,
              with_output=True):
        ensure_directory(project_dir)
        for d in ['code_objects', 'results']:
            ensure_directory(os.path.join(project_dir, d))

        # Write the arrays
        arr_tmp = CPPStandaloneCodeObject.templater.objects(None,
                                                            array_specs=self.array_specs,
                                                            dynamic_array_specs=self.dynamic_array_specs,
                                                            zero_specs=self.zero_specs,
                                                            arange_specs=self.arange_specs,
                                                            synapses=self.synapses,
                                                            )
        open(os.path.join(project_dir, 'objects.cpp'), 'w').write(arr_tmp.cpp_file)
        open(os.path.join(project_dir, 'objects.h'), 'w').write(arr_tmp.h_file)

        main_lines = []
        for func, args in self.main_queue:
            if func=='run_code_object':
                codeobj, = args
                main_lines.append('_run_%s(t);' % codeobj.name)
            elif func=='run_network':
                net, duration, namespace = args
                net._prepare_for_device(namespace)
                # Extract all the CodeObjects
                # Note that since we ran the Network object, these CodeObjects will be sorted into the right
                # running order, assuming that there is only one clock
                updaters = []
                for obj in net.objects:
                    for updater in obj.updaters:
                        updaters.append(updater)
                
                # Generate the updaters
                run_lines = []
                for updater in updaters:
                    cls = updater.__class__
                    if cls is CodeObjectUpdater:
                        codeobj = updater.owner
                        run_lines.append('_run_%s(t);' % codeobj.name)
                    else:
                        raise NotImplementedError("C++ standalone device has not implemented "+cls.__name__)
                    
                # Generate the main lines
                num_steps = int(duration/defaultclock.dt)
                netcode = CPPStandaloneCodeObject.templater.network(None, run_lines=run_lines, num_steps=num_steps)
                main_lines.extend(netcode.split('\n'))
            else:
                raise NotImplementedError("Unknown main queue function type "+func)

        # generate the finalisations
        for codeobj in self.code_objects.itervalues():
            if hasattr(codeobj.code, 'main_finalise'):
                main_lines.append(codeobj.code.main_finalise);

        # Generate data for non-constant values
        code_object_defs = defaultdict(list)
        already_deffed = defaultdict(set)
        for codeobj in self.code_objects.itervalues():
            for k, v in codeobj.variables.iteritems():
                if k=='t':
                    pass
                elif isinstance(v, Subexpression):
                    pass
                elif isinstance(v, AttributeVariable):
                    c_type = c_data_type(v.dtype)
                    # TODO: Handle dt in the correct way
                    if v.attribute == 'dt_':
                        code = ('const {c_type} {k} = '
                                '{value};').format(c_type=c_type,
                                                  k=k,
                                                  value=v.get_value())
                    else:
                        code = ('const {c_type} {k} = '
                                '{name}.{attribute};').format(c_type=c_type,
                                                             k=k,
                                                             name=v.obj.name,
                                                             attribute=v.attribute)
                    code_object_defs[codeobj.name].append(code)
                elif not v.scalar:
                    if hasattr(v, 'arrayname') and v.arrayname in already_deffed[codeobj.name]:
                        continue
                    try:
                        if isinstance(v, StandaloneDynamicArrayVariable):
                            code_object_defs[codeobj.name].append('const int _num{k} = _dynamic{arrayname}.size();'.format(k=k, arrayname=v.arrayname))
                            c_type = c_data_type(v.dtype)
                            # Create an alias name for the underlying array
                            code = ('{c_type}* {arrayname} = '
                                    '&(_dynamic{arrayname}[0]);').format(c_type=c_type,
                                                                          arrayname=v.arrayname)
                            code_object_defs[codeobj.name].append(code)
                            already_deffed[codeobj.name].add(v.arrayname)
                        else:
                            N = v.get_len()#len(v)
                            code_object_defs[codeobj.name].append('const int _num%s = %s;' % (k, N))
                    except TypeError:
                        pass

        # Generate the code objects
        for codeobj in self.code_objects.itervalues():
            ns = codeobj.namespace
            # TODO: fix these freeze/CONSTANTS hacks somehow - they work but not elegant.
            code = freeze(codeobj.code.cpp_file, ns)
            code = code.replace('%CONSTANTS%', '\n'.join(code_object_defs[codeobj.name]))
            code = '#include "objects.h"\n'+code
            
            open(os.path.join(project_dir, 'code_objects', codeobj.name+'.cpp'), 'w').write(code)
            open(os.path.join(project_dir, 'code_objects', codeobj.name+'.h'), 'w').write(codeobj.code.h_file)
                    
        # The code_objects are passed in the right order to run them because they were
        # sorted by the Network object. To support multiple clocks we'll need to be
        # smarter about that.
        main_tmp = CPPStandaloneCodeObject.templater.main(None,
                                                          main_lines=main_lines,
                                                          code_objects=self.code_objects.values(),
                                                          dt=float(defaultclock.dt),
                                                          )
        open(os.path.join(project_dir, 'main.cpp'), 'w').write(main_tmp)

        # Copy the brianlibdirectory
        brianlib_dir = os.path.join(os.path.split(inspect.getsourcefile(CPPStandaloneCodeObject))[0],
                                    'brianlib')
        copy_directory(brianlib_dir, os.path.join(project_dir, 'brianlib'))
        
        # build the project
        if compile_project:
            with in_directory(project_dir):
                if debug:
                    x = os.system('g++ -I. -g *.cpp code_objects/*.cpp -o main')
                else:
                    x = os.system('g++ -I. -O3 -ffast-math -march=native *.cpp code_objects/*.cpp -o main')
                if x==0:
                    if run_project:
                        if not with_output:
                            stdout = open(os.devnull, 'w')
                        else:
                            stdout = None
                        if os.name=='nt':
                            x = subprocess.call('main', stdout=stdout)
                        else:
                            x = subprocess.call('./main', stdout=stdout)
                        if x:
                            raise RuntimeError("Project run failed")
                else:
                    raise RuntimeError("Project compilation failed")


cpp_standalone_device = CPPStandaloneDevice()

all_devices['cpp_standalone'] = cpp_standalone_device

build = cpp_standalone_device.build

    
class Network(OrigNetwork):
    def run(self, duration, report=None, report_period=60*second,
            namespace=None, level=0):
        if namespace is None:
            namespace = get_local_namespace(1 + level)
        cpp_standalone_device.main_queue.append(('run_network', (self, duration, namespace)))
        
    def _prepare_for_device(self, namespace):
        OrigNetwork.run(self, 0*second, namespace=namespace)

def run(*args, **kwds):
    raise NotImplementedError("Magic networks not implemented for C++ standalone")
stop = run
reinit = run


class Synapses(OrigSynapses):
    def __init__(self, *args, **kwds):
        OrigSynapses.__init__(self, *args, **kwds)
        cpp_standalone_device.synapses.append(weakref.proxy(self))