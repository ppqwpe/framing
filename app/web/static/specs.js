export const specs={
compare:['선택한 순서대로 A와 B를 대조합니다. 영상 두 개 또는 이미지 두 개를 골라주세요.',[['mode','보기','side:나란히|wipe:좌우 나누기|overlay:겹쳐 보기|flicker:번갈아 보기|a:A만|b:B만'],['audio','소리','a:A 소리|b:B 소리|none:소리 없음'],['format','결과','video:영상|frame:한 장|frames:보기별 한 장'],['views','저장할 보기','current:선택한 보기|all:모든 보기'],['time','캡처 위치(초)','number',0],['split','좌우 나눌 위치(0~1)','number',.5],['mix','겹치기 비율(0~1)','number',.5]]],
extract:['영상에서 필요한 프레임을 이미지로 저장합니다.',[['pick','뽑는 방법','count:고르게 뽑기|all:전체 프레임|at:위치 직접 입력'],['count','이미지 수','number',5],['at','위치(초 또는 first middle last)','text','first middle last'],['frame_format','이미지 형식','png:PNG|jpg:JPG|exr:EXR (전체)|dpx:DPX (전체)']]],
retime:['목표 프레임 수에 맞춰 영상 길이를 조절합니다. 되돌릴 때 쓸 메모도 함께 저장합니다.',[['retime_mode','방법','motion:FFmpeg 모션 보간|rife:RIFE AI 보간|dup:프레임 복제|blend:프레임 혼합|hold:프레임 유지'],['target_frames','목표 프레임 수','number',264]]],
revert:['리타임한 영상과 함께 나온 메모를 올려 함께 선택하세요. 원본 프레임이 보존된 결과만 되돌릴 수 있습니다.',[]],
cuts:['장면 변화나 정한 간격을 기준으로 영상을 나눕니다.',[['cut_mode','분할 방법','interval:일정 간격|auto:장면 변화|manual:시간 직접 입력'],['cut_interval','간격(초)','number',3],['cut_threshold','장면 변화 감도 기준','number',10],['cut_times','자를 시간(초, 쉼표 구분)','text','3,6']]],
reverse:['영상의 프레임 순서를 뒤집어 저장합니다.',[]],
audit:['검은 화면·정지 구간 등 확인할 후보를 찾아 보고서로 저장합니다. 최종 판단은 결과를 보고 해주세요.',[['audit_freeze_seconds','정지 구간 기준(초)','number',.5]]],
contact:['주요 프레임을 한 장에 모아 저장합니다. 여러 영상을 한눈에 살펴볼 때 씁니다.',[]],
convert:['영상 형식을 바꾸거나 이미지들을 영상으로 묶습니다.',[['convert_to','변환','mp4:MP4|mov:MOV|small:용량 줄인 MP4|gif:GIF|image:이미지 변환|movie:이미지를 영상으로'],['image_format','이미지 형식','png:PNG|jpg:JPG|webp:WebP|exr:EXR|dpx:DPX'],['fps','이미지 영상 FPS','number',24],['gif_width','GIF 너비','number',640],['gif_fps','GIF FPS','number',12]]],
rename:['선택한 파일의 복사본에 새 이름을 붙여 ZIP으로 받습니다. 번호는 목록 순서대로 붙습니다.',[['style','방법','number:이름 + 번호|replace:글자 바꾸기|affix:앞뒤 글자 붙이기'],['name','공통 이름','text','SHOT'],['start','시작 번호','number',1],['width','번호 자릿수','number',3],['find','찾을 글자','text',''],['replace','바꿀 글자','text',''],['prefix','앞에 붙일 글자','text',''],['suffix','뒤에 붙일 글자','text','']]]};
