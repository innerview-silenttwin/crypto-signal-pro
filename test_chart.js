const fs = require('fs');
const data = JSON.parse(fs.readFileSync('test_data.json', 'utf8'));
const uniqueData = [];
let lastTimeStr = "";
let lastTimeSec = 0;
let tf = '1d';

for (let i = 0; i < data.length; i++) {
    const item = data[i];
    let lwcTime;
    if (tf === '1d') {
        const d = new Date(item.time * 1000);
        lwcTime = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
        if (lwcTime !== lastTimeStr) {
            lastTimeStr = lwcTime;
            uniqueData.push({ time: lwcTime, open: item.open, high: item.high, low: item.low, close: item.close });
        }
    } else {
        lwcTime = item.time;
        if (lwcTime > lastTimeSec) {
            lastTimeSec = lwcTime;
            uniqueData.push({ time: lwcTime, open: item.open, high: item.high, low: item.low, close: item.close });
        }
    }
}
console.log(uniqueData.slice(0, 3));
console.log(uniqueData.length);

// Check if sorted
let sorted = true;
for(let i=1; i<uniqueData.length; i++){
    if(tf === '1d'){
        if(new Date(uniqueData[i].time) <= new Date(uniqueData[i-1].time)) sorted = false;
    } else {
        if(uniqueData[i].time <= uniqueData[i-1].time) sorted = false;
    }
}
console.log("Is sorted?", sorted);
