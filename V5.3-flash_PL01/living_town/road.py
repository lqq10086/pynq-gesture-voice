"""Fixed two-row road and bounded car footprints; no collision simulation.

The billboard is anchored at the centre of its ground footprint, not its
bottom edge. These conservative extents enclose the authored tyre contacts
and bumpers in the retained 35/28 degree orthographic view.
"""
import math

ROAD_TOP=4./6.
ROAD_BOTTOM=1.
ROAD_CENTER=5./6.
CAR_ANCHOR=(.42,.69)
TYRE_CONTACTS=((.09,.603),(.525,.988))
SIZE_FACTORS=(.78,1.,1.22)

def _value(number):
    if type(number) not in (int,float) or not math.isfinite(number):
        raise ValueError('car coordinates must be finite numbers')
    return float(number)

def car_extents(size_index=1):
    if type(size_index) is not int or not 0<=size_index<len(SIZE_FACTORS):
        raise ValueError('invalid car size')
    scale=SIZE_FACTORS[size_index]
    return .12*scale,.095*scale

def clamp_car_position(x,y,size_index=1):
    x,y=_value(x),_value(y)
    half_x,half_y=car_extents(size_index)
    # One lane, with the whole footprint inside both road edges.
    return min(1.-half_x,max(half_x,x)),ROAD_CENTER

def snap_car_position(x,y,size_index=1):
    x,y=clamp_car_position(x,y,size_index)
    half_x,_=car_extents(size_index)
    first=math.ceil(half_x*8.-.5)
    last=math.floor((1.-half_x)*8.-.5)
    column=min(last,max(first,math.floor(x*8.)))
    return (column+.5)/8.,ROAD_CENTER

def car_footprint(x,y,size_index=1):
    half_x,half_y=car_extents(size_index)
    return ((x-half_x,y-half_y),(x+half_x,y-half_y),
            (x+half_x,y+half_y),(x-half_x,y+half_y))

def car_route(size_index=1):
    half_x,_=car_extents(size_index)
    return (1.-half_x,ROAD_CENTER),(half_x,ROAD_CENTER)
