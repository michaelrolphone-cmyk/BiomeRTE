"""Measure the photographed rover against the supplied 20mm floor grid.

Image landmarks are explicit and reviewable; heights are supplied by the owner.
Ground-plane homography and height-parallax correction are separate operations.
"""
import json
import numpy as np

# Visible checker corners only; wheel/wire-covered corners are excluded.
GRID_PIXELS=np.array([[828,338],[887,338],[886,398],[823,396],
                      [812,704],[873,707],[869,767],[811,764],
                      [995,774],[935,771]],float)
GRID_MM=np.array([[0,0],[20,0],[20,20],[0,20],
                  [0,120],[20,120],[20,140],[0,140],
                  [60,140],[40,140]],float)

def homography(source,target):
    rows=[]
    for (x,y),(u,v) in zip(source,target):
        rows.extend([[-x,-y,-1,0,0,0,u*x,u*y,u],
                     [0,0,0,-x,-y,-1,v*x,v*y,v]])
    _,_,vt=np.linalg.svd(rows)
    h=vt[-1].reshape(3,3)
    return h/h[2,2]

def project(h,points):
    points=np.atleast_2d(points)
    q=np.c_[points,np.ones(len(points))]@h.T
    return q[:,:2]/q[:,2,None]

def measurements():
    image_to_ground=homography(GRID_PIXELS,GRID_MM)
    grid_fit=project(np.linalg.inv(image_to_ground),GRID_MM)
    error=np.linalg.norm(grid_fit-GRID_PIXELS,axis=1)
    # Centers of front/rear wheel silhouettes at each side of the rover.
    wheels=np.array([[692,349],[1119,368],[679,733],[1110,754]],float)
    ground_wheels=project(image_to_ground,wheels)
    spans=[float(np.linalg.norm(ground_wheels[b]-ground_wheels[a])) for a,b in [(0,1),(2,3)]]
    wheelbase_mm=127.
    wheel_axis_height_mm=28.575
    heights=[wheel_axis_height_mm/(1-wheelbase_mm/span) for span in spans]
    phone_height_mm=float(np.mean(heights))
    # Near-overhead photograph: the image-center ground intersection supplies
    # the nominal camera nadir. The fit retains the approximation explicitly.
    nadir=project(image_to_ground,[[768,576]])[0]
    landmarks={'camera':([710,533],63.5),
               'front':([579,515],25.4),
               'right':([877,401],25.4),
               'left':([870,690],25.4),
               'rear':([1270,552],50.8)}
    corrected={}
    for name,(pixel,height) in landmarks.items():
        ground=project(image_to_ground,[pixel])[0]
        corrected[name]=(1-height/phone_height_mm)*ground+(height/phone_height_mm)*nadir
    forward=(ground_wheels[0]+ground_wheels[2]-ground_wheels[1]-ground_wheels[3])/2
    forward/=np.linalg.norm(forward)
    right=np.array([-forward[1],forward[0]])
    origins={}
    for name,point in corrected.items():
        if name=='camera':continue
        delta=point-corrected['camera']
        origins[name]=[float(delta@right),63.5-landmarks[name][1],float(delta@forward)]
    return {'image':'03-1000017388.jpg','grid_square_mm':20.,
            'grid_landmarks_px':GRID_PIXELS.tolist(),'grid_landmarks_mm':GRID_MM.tolist(),
            'image_to_ground_mm':image_to_ground.tolist(),'grid_reprojection_rms_px':float(np.sqrt(np.mean(error**2))),
            'grid_reprojection_max_px':float(max(error)),
            'independent_100mm_ruler_measurement_mm':float(np.linalg.norm(project(image_to_ground,[[1027,861]])[0]-project(image_to_ground,[[714,841]])[0])),
            'wheelbase_ground_projection_mm':spans,'phone_height_inferred_mm':phone_height_mm,
            'sensor_landmarks_px':{k:v[0] for k,v in landmarks.items()},
            'origin_camera_mm':origins,
            'height_parallax_model':'known sensor heights; wheelbase/axle-height camera-height estimate; image-center nadir approximation',
            'measurement_status':'photo_measured_with_explicit_parallax_model_not_precision_metrology'}

if __name__=='__main__':print(json.dumps(measurements(),indent=2))
