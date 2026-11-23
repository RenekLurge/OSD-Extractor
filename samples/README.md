# samples 说明

真实视频素材（含版权）不入仓库。请把素材放在本目录，注意：

1. **文件名/路径必须为英文/数字**（cv2 不支持中文路径）
2. 支持 .mp4 / .avi / .mov / .mkv；.m4s 分片先用 ffmpeg 转封装：
   `ffmpeg -i in.m4s -c copy out.mp4`
3. 仓库内置 `demo_synthetic.jpg` 为合成演示图（白字 OSD，数值已知），可用于功能自测
