# 移液工站workshop

### 硬件

定制化搭建的三轴、电动移液枪（10 ~ 1000 μL）、带通信协议的天平、工作区底板、枪头盒（枪头96个，容量均为1000 μL）、试管架（12个试管，容量15 mL）

### 点位

三轴可能移动到的位置（需要标定的点位）：

96个枪头位（8行12列，全部横平竖直）、12个试管位（2行6列，全部横平竖直）、天平接收瓶位（1个）

96个枪头位和12个试管位沿用E:\dev\works\Uni-Lab-OS-xyf1里面E:\dev\works\Uni-Lab-OS-xyf1\unilabos\test\experiments\greenlab_electrochem_01.json 和 E:\dev\works\Uni-Lab-OS-xyf1\unilabos\devices\liquid_handling\laiyu\config\electro_deck.json

注意：物料点位系统有两套坐标，有点乱，请你尝试统一到一套里面。

天平接收瓶位待标定

### 通信

电脑与设备之间的通信全部通过串口实现。要连接的串口（3个）：

三轴电机（三个设备共用一个串口）、移液器、天平

### 动作和软件

三轴移液设备有一个适配旧版uni-lab的控制体系，在E:\dev\works\Uni-Lab-OS-xyf1\unilabos\devices\liquid_handling\laiyu
希望迁移到新的uni-lab上，位置E:\dev\works\Uni-Lab-OS_new_05

移液器的参数设置指令见"E:\dev\works\liquid-handling-workshop\liquid\SOPA气动式移液器RS485控制指令.md"

天平的控制脚本请阅读"E:\dev\works\liquid-handling-workshop\RS232脚本使用说明.txt"

界面展示：需要记录下每次移液的吸液量（体积）、实测质量、密度、换算出来的实测体积；需要看到当前正在执行的工作步骤，可以方便地选择从哪个试管位移液。如果当前的uni-lab可以实现，就用uni-lab实现；否则，自己写前后端。

### 流程

要实现的工作流程及目标：

移液器的参数取默认值不变，分别从试管架上的试管里移取30\70\150\300\500\800 μL的水、甘油溶液、蔗糖溶液到称量位。天平自动读取稳定后的质量，并去皮。然后根据设置好的溶液的密度计算出实际移取的体积。得到三条x实测值-y吸液量的线性拟合曲线。接着从三个未知样的试管里分别移取液体到称量位（具体移多少次、移多少体积由现场参与者决定），根据曲线的符合程度，确定未知样各自是什么。最后将预期实际吸液量200μL代回曲线，确定移液器的移液体积应该设置多少，再实际移液，检验误差。
